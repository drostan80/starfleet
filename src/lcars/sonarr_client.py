"""Sonarr metadata fetch (+ write, B.21) — SCOPE.md §5.1/§5.2, BUILD_PLAN.md A.8/B.21.

A close port of Data's own real, working `SonarrClient`
(~/repos/data/src/data/sonarr.py) — same error-handling shape
(SonarrError, 401-specific message, connect/timeout distinction).
Originally narrowed to just the two read calls A.8 needed (find an
already-in-Sonarr-library series by tvdb id, and its episode list);
B.21 ports the rest of Data's own real client — the search/add/update
methods — so LCARS itself can add a show to Sonarr and unmonitor one on
drop, rather than that being something only Data can do (see
BUILD_PLAN.md B.21 for the full design). Adapted to LCARS's own sync
execution model (§11.2) — a sync `httpx.Client`, not Data's
`httpx.AsyncClient`. A show being tracked in LCARS does not imply it's
in Sonarr's own library (§5.1: "tv's Sonarr link is optional") —
`series_by_tvdb_id` returning `None` means exactly that, not an error.
"""

import httpx


class SonarrError(Exception):
    pass


class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=f"{self.base_url}/api/v3/",
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SonarrClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> object:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise SonarrError(f"Could not connect to Sonarr at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise SonarrError("Sonarr rejected the API key (401 Unauthorized)") from e
            raise SonarrError(f"Sonarr returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise SonarrError(f"Timed out talking to Sonarr at {self.base_url}") from e
        return response.json()

    def _post(self, path: str, json: dict) -> object:
        """B.21 — the write half, close port of Data's own real, already-
        shipping `SonarrClient._post` (~/repos/data/src/data/sonarr.py).
        Same error shape as `_get`, plus Sonarr's own validation-failure
        body: a 400 on a bad add (duplicate series, bad root folder path,
        etc.) comes back as a JSON array of `{propertyName, errorMessage}`
        objects, not a single message — worth surfacing directly."""
        try:
            response = self._client.post(path, json=json)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise SonarrError(f"Could not connect to Sonarr at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise SonarrError("Sonarr rejected the API key (401 Unauthorized)") from e
            detail = None
            try:
                body = e.response.json()
                if isinstance(body, list):
                    detail = "; ".join(
                        item.get("errorMessage", "") for item in body if item.get("errorMessage")
                    )
                elif isinstance(body, dict):
                    detail = body.get("message")
            except ValueError:
                pass
            if detail:
                raise SonarrError(f"Sonarr rejected the request: {detail}") from e
            raise SonarrError(f"Sonarr returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise SonarrError(f"Timed out talking to Sonarr at {self.base_url}") from e
        return response.json()

    def _put(self, path: str, json: dict) -> object:
        """B.21 — same shape as `_get`; Sonarr's update endpoint doesn't
        document the same validation-error body `_post`'s add endpoint
        does, so no detail-extraction here (matches Data's own real
        client, which doesn't do it for `_put` either) — a real 400 still
        surfaces as `Sonarr returned an error: HTTP 400`, not lost."""
        try:
            response = self._client.put(path, json=json)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise SonarrError(f"Could not connect to Sonarr at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise SonarrError("Sonarr rejected the API key (401 Unauthorized)") from e
            raise SonarrError(f"Sonarr returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise SonarrError(f"Timed out talking to Sonarr at {self.base_url}") from e
        return response.json()

    def series_by_tvdb_id(self, tvdb_id: int) -> dict | None:
        """The matching series already in Sonarr's own library, or None
        if this tvdb_id isn't tracked there at all — not an error."""
        results = self._get("series", params={"tvdbId": tvdb_id})
        return results[0] if results else None

    def all_series(self) -> list[dict]:
        """§5.2/§6.10, B.3b — every series Sonarr's own library tracks,
        tvdbId/path included on each — the "does Sonarr know about a
        show LCARS doesn't track at all" half of the local-file audit
        (local_audit.py). No params: Sonarr's own `/series` with no
        filter returns the whole library in one call."""
        return self._get("series")

    def episodes(self, series_id: int, include_episode_file: bool = False) -> list[dict]:
        """`include_episode_file=True` (§5.2/§6.10, B.3b) embeds each
        episode's current `episodeFile` (path included) directly when
        `hasFile` is true — verified live: this is Sonarr's own
        *current* state, independent of and a genuine cross-check
        against the `/history` event log availability.py (B.3) already
        polls. Defaults False — A.8's existing metadata-fetch caller
        doesn't need the extra payload."""
        params = {"seriesId": series_id}
        if include_episode_file:
            params["includeEpisodeFile"] = "true"
        return self._get("episode", params=params)

    def lookup_series(self, term: str) -> list[dict]:
        """B.21 — Sonarr's own "add new show" search, already TVDB-backed
        and pre-shaped for `add_series()`. `term` can be a plain title
        (free-text search, ranked by relevance) or `"tvdb:{id}"` for an
        exact-id lookup — confirmed live against the real deployment: an
        id-form term returns exactly one candidate."""
        return self._get("series/lookup", params={"term": term})

    def root_folders(self) -> list[dict]:
        """B.21 — not used on the add path itself (LCARS uses its own
        configured default root folder, see config.py's
        sonarr_anime_root_folder/sonarr_tv_root_folder) — this exists for
        a config-validation check (confirm the configured path is real),
        not a per-add runtime lookup."""
        return self._get("rootfolder")

    def quality_profiles(self) -> list[dict]:
        """B.21 — same "config validation only" reasoning as
        root_folders() above, not called on the add path itself."""
        return self._get("qualityprofile")

    def add_series(self, payload: dict) -> dict:
        """B.21 — the actual write. Caller builds `payload` from a
        lookup_series() candidate plus LCARS's own configured root
        folder/quality profile defaults — see resolvers.py's
        addShowWithArr for the real shape."""
        return self._post("series", json=payload)

    def update_series(self, series: dict) -> dict:
        """B.21 — PUT the full series object back; Sonarr's update
        endpoint wants the whole thing, not a partial patch. Caller
        mutates `series` (e.g. `series["monitored"]`, an entry in
        `series["seasons"]`) before calling — used for the
        auto-unmonitor-on-drop path (resolvers.py)."""
        return self._put(f"series/{series['id']}", json=series)

    def history_page(self, page: int, page_size: int = 250) -> dict:
        """§5.2/§6.7, B.3 — one page of Sonarr's own grab/import event
        log, newest first. `includeEpisode`/`includeSeries` embed the
        full episode (seasonNumber/episodeNumber, and — 2026-08-24 —
        absoluteEpisodeNumber, real live availability.py bug fix,
        needed to route a tvdb id shared by more than one LCARS show)
        and series (tvdbId) objects directly on each record — verified
        live against a real Sonarr instance (4.0.19.2979) before this
        was written: this is enough to match an event straight to
        LCARS's own show_external_id crosswalk (§5.4), no separate
        seriesId->tvdbId lookup or new correlation-id column needed.
        Real event types seen: `grabbed`, `downloadFolderImported`
        (data.importedPath is the file's actual path),
        `episodeFileDeleted`, `downloadIgnored` (a rejected grab — no
        availability effect)."""
        return self._get(
            "history",
            params={
                "page": page,
                "pageSize": page_size,
                "sortKey": "date",
                "sortDirection": "descending",
                "includeEpisode": "true",
                "includeSeries": "true",
            },
        )
