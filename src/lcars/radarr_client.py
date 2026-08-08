"""Radarr metadata fetch — SCOPE.md §5.1, BUILD_PLAN.md A.8.

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

    def movie_by_tmdb_id(self, tmdb_id: int) -> dict | None:
        """The matching movie already in Radarr's own library, or None
        if this tmdb_id isn't tracked there at all — not an error."""
        results = self._get("movie", params={"tmdbId": tmdb_id})
        return results[0] if results else None
