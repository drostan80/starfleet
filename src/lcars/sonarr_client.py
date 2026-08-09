"""Sonarr metadata fetch — SCOPE.md §5.1/§5.2, BUILD_PLAN.md A.8.

A close port of Data's own real, working `SonarrClient`
(~/repos/data/src/data/sonarr.py) — same error-handling shape
(SonarrError, 401-specific message, connect/timeout distinction) —
narrowed to just the two calls A.8 actually needs (find an
already-in-Sonarr-library series by tvdb id, and its episode list).
Adapted to LCARS's own sync execution model (§11.2) — a sync
`httpx.Client`, not Data's `httpx.AsyncClient`. A show being tracked in
LCARS does not imply it's in Sonarr's own library (§5.1: "tv's Sonarr
link is optional") — `series_by_tvdb_id` returning `None` means
exactly that, not an error.
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

    def series_by_tvdb_id(self, tvdb_id: int) -> dict | None:
        """The matching series already in Sonarr's own library, or None
        if this tvdb_id isn't tracked there at all — not an error."""
        results = self._get("series", params={"tvdbId": tvdb_id})
        return results[0] if results else None

    def episodes(self, series_id: int) -> list[dict]:
        return self._get("episode", params={"seriesId": series_id})

    def history_page(self, page: int, page_size: int = 250) -> dict:
        """§5.2/§6.7, B.3 — one page of Sonarr's own grab/import event
        log, newest first. `includeEpisode`/`includeSeries` embed the
        full episode (seasonNumber/episodeNumber) and series (tvdbId)
        objects directly on each record — verified live against a real
        Sonarr instance (4.0.19.2979) before this was written: this is
        enough to match an event straight to LCARS's own
        show_external_id crosswalk (§5.4), no separate seriesId->tvdbId
        lookup or new correlation-id column needed. Real event types
        seen: `grabbed`, `downloadFolderImported` (data.importedPath is
        the file's actual path), `episodeFileDeleted`,
        `downloadIgnored` (a rejected grab — no availability effect)."""
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
