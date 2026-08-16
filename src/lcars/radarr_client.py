"""Radarr metadata fetch (+ write, B.21) — SCOPE.md §5.1, BUILD_PLAN.md A.8/B.21.

No aniq/Data reference exists for this one (aniq is anime/Sonarr-only —
confirmed by checking, no radarr.py anywhere in that repo) — built
fresh, following the same *arr-family REST conventions
`sonarr_client.py` already uses (X-Api-Key header, `/api/v3/` prefix,
a `?tmdbId=` lookup against the service's own library rather than a
public search — Radarr's real API surface, structurally near-identical
to Sonarr's own `/series`). A show being tracked in LCARS does not
imply it's in Radarr's own library — `movie_by_tmdb_id` returning
`None` means exactly that, not an error, same as
`sonarr_client.series_by_tvdb_id`.

B.21 adds real write support (`lookup_movie`/`add_movie`/`update_movie`),
full parity with `sonarr_client.py`'s own B.21 additions even though
nothing calls the Radarr side yet (no movie has ever been added through
this codebase — user's own explicit instruction: build it anyway,
closing the schema's already-modeled `media_shape = MOVIE` gap
symmetrically). The add payload shape (`_post`'s error-detail
extraction included) is **not independently verified against a real
Radarr 400 response** the way Sonarr's is — inferred from the same
shared Servarr framework both services are built on, plus a live,
read-only `movie/lookup?term=tmdb:27205` check confirming the
not-yet-owned candidate shape (`qualityProfileId: 0`, `movieFileId: 0`,
`monitored: false`, `minimumAvailability` present, no `path`/`id` key
at all) before this was written. If Radarr's real validation-error body
ever turns out to differ, `_post`'s detail-extraction degrades
gracefully to the bare `HTTP {status}` fallback either way — never a
silent parse crash.
"""

import httpx


class RadarrError(Exception):
    pass


class RadarrClient:
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

    def __enter__(self) -> "RadarrClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> object:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise RadarrError(f"Could not connect to Radarr at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise RadarrError("Radarr rejected the API key (401 Unauthorized)") from e
            raise RadarrError(f"Radarr returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise RadarrError(f"Timed out talking to Radarr at {self.base_url}") from e
        return response.json()

    def _post(self, path: str, json: dict) -> object:
        """B.21 — mirrors sonarr_client.py's own `_post` exactly (see
        this module's own docstring for the "not independently verified"
        caveat on the validation-error body shape specifically)."""
        try:
            response = self._client.post(path, json=json)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise RadarrError(f"Could not connect to Radarr at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise RadarrError("Radarr rejected the API key (401 Unauthorized)") from e
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
                raise RadarrError(f"Radarr rejected the request: {detail}") from e
            raise RadarrError(f"Radarr returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise RadarrError(f"Timed out talking to Radarr at {self.base_url}") from e
        return response.json()

    def _put(self, path: str, json: dict) -> object:
        try:
            response = self._client.put(path, json=json)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise RadarrError(f"Could not connect to Radarr at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise RadarrError("Radarr rejected the API key (401 Unauthorized)") from e
            raise RadarrError(f"Radarr returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise RadarrError(f"Timed out talking to Radarr at {self.base_url}") from e
        return response.json()

    def lookup_movie(self, term: str) -> list[dict]:
        """B.21 — Radarr's own "add new movie" search, TMDB-backed and
        pre-shaped for `add_movie()`. `term` can be a plain title or
        `"tmdb:{id}"` for an exact-id lookup — confirmed live: an id-form
        term returns exactly one candidate. A not-yet-owned candidate has
        no `id`/`path` key at all and `qualityProfileId: 0`/
        `movieFileId: 0`/`monitored: false` — real, live-confirmed shape,
        not guessed."""
        return self._get("movie/lookup", params={"term": term})

    def root_folders(self) -> list[dict]:
        """B.21 — config-validation only, same reasoning as
        sonarr_client.py's own root_folders()."""
        return self._get("rootfolder")

    def quality_profiles(self) -> list[dict]:
        """B.21 — config-validation only, same reasoning as
        sonarr_client.py's own quality_profiles()."""
        return self._get("qualityprofile")

    def add_movie(self, payload: dict) -> dict:
        """B.21 — the actual write. Caller builds `payload` from a
        lookup_movie() candidate plus LCARS's own configured root
        folder/quality profile defaults — see resolvers.py's
        addShowWithArr for the real shape."""
        return self._post("movie", json=payload)

    def update_movie(self, movie: dict) -> dict:
        """B.21 — PUT the full movie object back, same "whole object,
        not a partial patch" shape as sonarr_client.py's own
        update_series(). No season concept for a movie — used for the
        auto-unmonitor-on-drop path, movie-level `monitored` only."""
        return self._put(f"movie/{movie['id']}", json=movie)

    def movie_by_tmdb_id(self, tmdb_id: int) -> dict | None:
        """The matching movie already in Radarr's own library, or None
        if this tmdb_id isn't tracked there at all — not an error. Its
        own response already embeds `movieFile` (path included) when
        present — Radarr's *current* state, no separate include-flag or
        endpoint needed the way Sonarr's episodes() does (verified live:
        confirmed on a real /movie?tmdbId= response before B.3b was
        written)."""
        results = self._get("movie", params={"tmdbId": tmdb_id})
        return results[0] if results else None

    def all_movies(self) -> list[dict]:
        """§5.2/§6.10, B.3b — every movie Radarr's own library tracks,
        tmdbId/path/movieFile included on each — the "does Radarr know
        about a movie LCARS doesn't track at all" half of the
        local-file audit (local_audit.py)."""
        return self._get("movie")

    def history_page(self, page: int, page_size: int = 250) -> dict:
        """§5.2/§6.10, B.3 — Radarr's own grab/import event log, mirrors
        SonarrClient.history_page() exactly — verified live against a
        real Radarr instance (6.3.0.10514) before this was written.
        `includeMovie` embeds the full movie object (tmdbId) directly.
        Real event types seen: `grabbed`, `downloadFolderImported`
        (data.importedPath), `movieFileDeleted`. One real API quirk
        found by testing: the embedded movie's own `hasFile` field can
        come back null even when `movieFileId` is populated — the
        `eventType` itself is the authoritative signal here, not the
        embedded object's own hasFile."""
        return self._get(
            "history",
            params={
                "page": page,
                "pageSize": page_size,
                "sortKey": "date",
                "sortDirection": "descending",
                "includeMovie": "true",
            },
        )
