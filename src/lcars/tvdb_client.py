"""TVDB v4 API client — artwork retrieval (2026-08-30).

Fetches poster, banner, and background artwork for series and movies
from the TVDB v4 API.  Auth is JWT-based: ``POST /v4/login`` with the
API key mints a token (exp ~1 month); the client caches it in-process
and re-authenticates on 401.

Only artwork endpoints are used here — episode/series metadata comes
from AniList and Sonarr/Radarr.

TVDB artwork type IDs (from ``/v4/artwork/types``):
  Series: 1=banner, 2=poster, 3=background, 5=icon, 22=clearart, 23=clearlogo
  Season: 6=banner, 7=poster, 8=background, 10=icon
  Movie:  14=poster, 15=background, 16=banner, 18=icon
"""

from __future__ import annotations

import httpx

BASE_URL = "https://api4.thetvdb.com/v4"

# Map TVDB artwork type IDs to our art_asset.kind values.
_SERIES_TYPE_MAP = {1: "banner", 2: "poster", 3: "background"}
_SEASON_TYPE_MAP = {6: "banner", 7: "poster", 8: "background"}
_MOVIE_TYPE_MAP  = {14: "poster", 15: "background", 16: "banner"}


class TvdbError(Exception):
    pass


class TvdbClient:
    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._token: str | None = None
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TvdbClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------

    def _authenticate(self) -> None:
        """Obtain a JWT from TVDB.  Called once, then on 401 retry."""
        try:
            resp = self._client.post("/login", json={"apikey": self._api_key})
        except httpx.ConnectError as e:
            raise TvdbError("Could not connect to TVDB") from e
        except httpx.TimeoutException as e:
            raise TvdbError("Timed out talking to TVDB") from e
        if resp.status_code == 401:
            raise TvdbError("TVDB rejected the API key (401 Unauthorized)")
        if resp.status_code >= 400:
            raise TvdbError(f"TVDB login error: HTTP {resp.status_code}")
        data = resp.json()
        self._token = data.get("data", {}).get("token")
        if not self._token:
            raise TvdbError("TVDB login returned no token")

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """GET with automatic auth and single-retry on 401."""
        if self._token is None:
            self._authenticate()

        for attempt in range(2):
            try:
                resp = self._client.get(
                    path,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            except httpx.ConnectError as e:
                raise TvdbError("Could not connect to TVDB") from e
            except httpx.TimeoutException as e:
                raise TvdbError("Timed out talking to TVDB") from e

            if resp.status_code == 401 and attempt == 0:
                self._authenticate()
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise TvdbError(f"TVDB error: HTTP {resp.status_code}")
            return resp.json()

        raise TvdbError("TVDB auth failed after retry")  # unreachable in practice

    # -- public API ---------------------------------------------------------

    def series_artworks(self, tvdb_id: int) -> list[dict]:
        """Fetch all artwork for a series, returning normalized dicts.

        Each dict: ``{kind, url, width, height, language, source_score,
        season_number}`` — ``season_number`` is non-null only for
        season-scoped art (type 7/6/8).

        The season mapping (tvdb seasonId → season number) is resolved
        from the series' extended info in the same request batch.
        """
        # 1. Get season mapping: tvdb seasonId → season number
        extended = self._get(f"/series/{tvdb_id}/extended")
        if not extended:
            return []
        season_map: dict[int, int] = {}
        for s in (extended.get("data") or {}).get("seasons") or []:
            stype = (s.get("type") or {}).get("name") or ""
            # Only "Aired Order" seasons — ignore "Absolute Order" etc.
            if "aired" in stype.lower() or stype == "":
                sid = s.get("id")
                snum = s.get("number")
                if sid is not None and snum is not None:
                    season_map[sid] = snum

        # 2. Get artworks
        art_resp = self._get(f"/series/{tvdb_id}/artworks")
        if not art_resp:
            return []
        raw_arts = (art_resp.get("data") or {}).get("artworks") or []

        results = []
        for art in raw_arts:
            art_type = art.get("type")
            url = art.get("image")
            if not url or art_type is None:
                continue

            kind = _SERIES_TYPE_MAP.get(art_type) or _SEASON_TYPE_MAP.get(art_type)
            if kind is None:
                continue  # icon, clearart, clearlogo, cinemagraph — skip

            season_number = None
            if art_type in _SEASON_TYPE_MAP:
                season_tvdb_id = art.get("seasonId")
                if season_tvdb_id is not None:
                    season_number = season_map.get(season_tvdb_id)
                    # Can't map to an LCARS season — skip rather than guess
                    if season_number is None:
                        continue

            results.append({
                "kind": kind,
                "url": url,
                "width": art.get("width"),
                "height": art.get("height"),
                "language": art.get("language"),
                "source_score": art.get("score"),
                "season_number": season_number,
            })

        return results

    def movie_artworks(self, tvdb_id: int) -> list[dict]:
        """Fetch all artwork for a movie, returning normalized dicts.

        Each dict: ``{kind, url, width, height, language, source_score}``.
        No season_number (movies have no seasons).
        """
        art_resp = self._get(f"/movies/{tvdb_id}/extended")
        if not art_resp:
            return []
        raw_arts = (art_resp.get("data") or {}).get("artworks") or []

        results = []
        for art in raw_arts:
            art_type = art.get("type")
            url = art.get("image")
            if not url or art_type is None:
                continue

            kind = _MOVIE_TYPE_MAP.get(art_type)
            if kind is None:
                continue

            results.append({
                "kind": kind,
                "url": url,
                "width": art.get("width"),
                "height": art.get("height"),
                "language": art.get("language"),
                "source_score": art.get("score"),
                "season_number": None,
            })

        return results
