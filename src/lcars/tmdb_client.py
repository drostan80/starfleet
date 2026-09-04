"""TMDB metadata fetch — SCOPE.md §5.1, BUILD_PLAN.md A.19 (fixes the
`show.duration_minutes` gap flagged, not fixed, during A.13: no
mutation sets it, and A.8's AniList/Radarr fetch never requested a
duration/runtime field). AniList already covers `tracking_space =
anime` (see anilist_client.py's own `duration` field, metadata.py's
`_fetch_anilist`) — this client covers everything else, per the user's
own direction: "tmdb should have it for every media for a one time
check" (2026-08-08).

Plain v3 API-key auth (`?api_key=`), not the v4 Bearer-token/project
auth — matches this project's existing Sonarr/Radarr REST-client
convention (simple credential, no OAuth dance) for what's read-only,
publicly-available metadata anyway.

Two lookups only, both idempotent and safe to call repeatedly (a
show's runtime essentially never changes, hence "one time check" —
this isn't wired into any recurring poll):
  - `movie_runtime`: a movie's tmdb id usually already exists (§5.4 —
    movies key primarily on TMDB, native to Radarr).
  - `tv_episode_runtime` + `find_by_tvdb_id`: a non-anime TV show
    usually only carries a tvdb id (native to Sonarr) — `find_by_tvdb_id`
    bridges TVDB->TMDB (TMDB's own `/find` endpoint, `external_source=
    tvdb_id`) so metadata.py can resolve and persist a real tmdb id
    the first time, same as if the caller had supplied one directly.
"""

import httpx

BASE_URL = "https://api.themoviedb.org/3"


class TmdbError(Exception):
    pass


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TmdbClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        request_params = dict(params or {})
        request_params["api_key"] = self.api_key
        try:
            response = self._client.get(path, params=request_params)
        except httpx.ConnectError as e:
            raise TmdbError("Could not connect to TMDB") from e
        except httpx.TimeoutException as e:
            raise TmdbError("Timed out talking to TMDB") from e
        if response.status_code == 404:
            # A real, legitimate "no such id" from TMDB itself — unlike
            # Radarr/Sonarr's own by-id lookups (200 + empty list), TMDB's
            # /movie, /tv, /find endpoints 404 outright for an unknown id.
            return None
        if response.status_code == 401:
            raise TmdbError("TMDB rejected the API key (401 Unauthorized)")
        if response.status_code >= 400:
            raise TmdbError(f"TMDB returned an error: HTTP {response.status_code}")
        return response.json()

    def find_by_tvdb_id(self, tvdb_id: int) -> int | None:
        """Resolves a TVDB id to TMDB's own id for the same TV show, or
        None if TMDB has no match. TMDB's `/find` endpoint returns
        several typed result buckets (`movie_results`, `tv_results`,
        `person_results`, ...) for one external id — only `tv_results`
        is relevant here, since this bridge exists specifically for
        Sonarr-linked (episodic) shows."""
        data = self._get(f"find/{tvdb_id}", params={"external_source": "tvdb_id"})
        results = (data or {}).get("tv_results") or []
        return results[0]["id"] if results else None

    def movie_runtime(self, tmdb_id: int) -> int | None:
        data = self._get(f"movie/{tmdb_id}")
        return (data or {}).get("runtime") or None

    def tv_season_synopses(self, tmdb_id: int, season_number: int) -> list[dict]:
        """Fetch episode overviews for one season of a TV show.

        Returns list of ``{season, episode, overview}`` dicts for episodes
        that have a non-empty ``overview``.
        """
        data = self._get(f"tv/{tmdb_id}/season/{season_number}", params={"language": "en-US"})
        if not data:
            return []
        results = []
        for ep in data.get("episodes") or []:
            overview = (ep.get("overview") or "").strip()
            if overview:
                results.append({
                    "season": ep.get("season_number", season_number),
                    "episode": ep.get("episode_number"),
                    "overview": overview,
                })
        return results

    def tv_synopsis(self, tmdb_id: int) -> str | None:
        """Fetch the show-level overview from TMDB (English)."""
        data = self._get(f"tv/{tmdb_id}", params={"language": "en-US"})
        if not data:
            return None
        return (data.get("overview") or "").strip() or None

    def tv_episode_runtime(self, tmdb_id: int) -> int | None:
        """TMDB's own odd shape: `episode_run_time` is a list (it can
        track runtime changes across a long-running show's history),
        not a single value — the last entry is used as the single
        "default per-show runtime" `duration_minutes` itself represents
        (§5.1), matching the most-recent/current episode length."""
        data = self._get(f"tv/{tmdb_id}")
        run_times = (data or {}).get("episode_run_time") or []
        return run_times[-1] if run_times else None

    # ── Discover (browse) ───────────────────────────────────

    def discover_tv(self, air_date_gte: str, air_date_lte: str,
                    page: int = 1) -> dict:
        """Discover TV shows with episodes airing in the given date range.

        ``air_date_gte``/``air_date_lte`` are ISO dates (YYYY-MM-DD).
        TMDB's ``air_date`` filter is episode-level: a show appears only
        if it has at least one episode dated within the range.

        Returns the raw TMDB Discover response (``page``, ``results``,
        ``total_pages``, ``total_results``).
        """
        data = self._get("discover/tv", params={
            "air_date.gte": air_date_gte,
            "air_date.lte": air_date_lte,
            "sort_by": "popularity.desc",
            "page": page,
        })
        return data or {"page": page, "results": [], "total_pages": 0,
                        "total_results": 0}

    def discover_movies(self, release_gte: str, release_lte: str,
                        page: int = 1) -> dict:
        """Discover movies with a primary release date in the given range.

        Returns the raw TMDB Discover response.
        """
        data = self._get("discover/movie", params={
            "primary_release_date.gte": release_gte,
            "primary_release_date.lte": release_lte,
            "sort_by": "popularity.desc",
            "page": page,
        })
        return data or {"page": page, "results": [], "total_pages": 0,
                        "total_results": 0}
