"""TV & Movies browse — ``browseTmdb`` query implementation.

Fetches TMDB's Discover endpoint for TV shows with episodes airing in
a given month and movies with a primary release date in that month,
cross-references against LCARS's own tracked shows (by ``tmdb`` entries
in ``show_external_id``), and returns combined metadata + LCARS status.

Module-level cache follows the same pattern as browse.py (anime seasonal).
"""

import calendar
import logging
import sqlite3
import time

from lcars import config, tmdb_client

log = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────

_cache: dict[tuple[str, int, int, int], tuple[float, dict]] = {}
_CACHE_TTL_PAST = 86400     # 24 hours for past months
_CACHE_TTL_CURRENT = 1800   # 30 min for current month
_CACHE_TTL_FUTURE = 900     # 15 min for future months


def _cache_ttl(year: int, month: int) -> int:
    import datetime
    now = datetime.date.today()
    cur = (now.year, now.month)
    req = (year, month)
    if req < cur:
        return _CACHE_TTL_PAST
    if req == cur:
        return _CACHE_TTL_CURRENT
    return _CACHE_TTL_FUTURE


def _get_cached(kind: str, year: int, month: int, page: int) -> dict | None:
    key = (kind, year, month, page)
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, data = entry
    if time.monotonic() - cached_at > _cache_ttl(year, month):
        del _cache[key]
        return None
    return data


def _set_cached(kind: str, year: int, month: int, page: int,
                data: dict) -> None:
    _cache[(kind, year, month, page)] = (time.monotonic(), data)


# ── Cross-reference ──────────────────────────────────────────


def _cross_reference(
    conn: sqlite3.Connection, tmdb_ids: list[int]
) -> dict[int, dict]:
    """Return {tmdb_id -> {show_id, status}} for tracked shows matching
    the given TMDB IDs via ``show_external_id``."""
    if not tmdb_ids:
        return {}

    result: dict[int, dict] = {}
    placeholders = ",".join("?" for _ in tmdb_ids)
    str_ids = [str(tid) for tid in tmdb_ids]
    rows = conn.execute(
        f"SELECT sei.external_id, sei.show_id, sh.status, sh.tracked"
        f" FROM show_external_id sei"
        f" JOIN show sh ON sh.id = sei.show_id"
        f" WHERE sei.service = 'tmdb' AND sei.external_id IN ({placeholders})",
        str_ids,
    ).fetchall()
    for row in rows:
        if not row["tracked"] and row["status"] != "skipped":
            continue  # ignore untracked stubs (but keep skipped tombstones)
        tid = int(row["external_id"])
        result[tid] = {
            "show_id": row["show_id"],
            "status": row["status"],
        }

    return result


# ── Flattening ───────────────────────────────────────────────


def _flatten_tv(item: dict, lcars_match: dict | None) -> dict:
    """Flatten a TMDB Discover TV result into the TmdbBrowseItem shape."""
    return {
        "tmdb_id": item["id"],
        "media_type": "TV",
        "title": item.get("name") or item.get("original_name") or "(Untitled)",
        "original_title": item.get("original_name"),
        "overview": item.get("overview") or None,
        "poster_url": (
            f"https://image.tmdb.org/t/p/w300{item['poster_path']}"
            if item.get("poster_path") else None
        ),
        "backdrop_url": (
            f"https://image.tmdb.org/t/p/w780{item['backdrop_path']}"
            if item.get("backdrop_path") else None
        ),
        "first_air_date": item.get("first_air_date"),
        "release_date": None,
        "popularity": item.get("popularity"),
        "vote_average": item.get("vote_average"),
        "genre_ids": item.get("genre_ids") or [],
        "lcars_show_id": lcars_match["show_id"] if lcars_match else None,
        "lcars_status": lcars_match["status"] if lcars_match else None,
    }


def _flatten_movie(item: dict, lcars_match: dict | None) -> dict:
    """Flatten a TMDB Discover Movie result into the TmdbBrowseItem shape."""
    return {
        "tmdb_id": item["id"],
        "media_type": "MOVIE",
        "title": item.get("title") or item.get("original_title") or "(Untitled)",
        "original_title": item.get("original_title"),
        "overview": item.get("overview") or None,
        "poster_url": (
            f"https://image.tmdb.org/t/p/w300{item['poster_path']}"
            if item.get("poster_path") else None
        ),
        "backdrop_url": (
            f"https://image.tmdb.org/t/p/w780{item['backdrop_path']}"
            if item.get("backdrop_path") else None
        ),
        "first_air_date": None,
        "release_date": item.get("release_date"),
        "popularity": item.get("popularity"),
        "vote_average": item.get("vote_average"),
        "genre_ids": item.get("genre_ids") or [],
        "lcars_show_id": lcars_match["show_id"] if lcars_match else None,
        "lcars_status": lcars_match["status"] if lcars_match else None,
    }


# ── Main entry points ───────────────────────────────────────


def _date_range(year: int, month: int) -> tuple[str, str]:
    """Return (first_day, last_day) as ISO date strings for the month."""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


def _get_client() -> tmdb_client.TmdbClient:
    cfg = config.get_current()
    if not cfg.tmdb_api_key:
        raise tmdb_client.TmdbError("TMDB API key not configured")
    return tmdb_client.TmdbClient(cfg.tmdb_api_key)


def fetch_tmdb_tv(
    conn: sqlite3.Connection, year: int, month: int, page: int = 1
) -> dict:
    """Fetch TV shows with episodes airing in the given month."""
    cached = _get_cached("tv", year, month, page)
    if cached is not None:
        tmdb_page = cached
    else:
        gte, lte = _date_range(year, month)
        with _get_client() as client:
            tmdb_page = client.discover_tv(gte, lte, page)
        _set_cached("tv", year, month, page, tmdb_page)

    results = tmdb_page.get("results") or []

    # Cross-reference
    tmdb_ids = [r["id"] for r in results]
    lcars_matches = _cross_reference(conn, tmdb_ids)

    items = [_flatten_tv(r, lcars_matches.get(r["id"])) for r in results]

    return {
        "items": items,
        "current_page": tmdb_page.get("page", page),
        "total_pages": tmdb_page.get("total_pages", 1),
        "total_results": tmdb_page.get("total_results", len(items)),
        "has_next_page": tmdb_page.get("page", page) < tmdb_page.get(
            "total_pages", 1),
    }


def fetch_tmdb_movies(
    conn: sqlite3.Connection, year: int, month: int, page: int = 1
) -> dict:
    """Fetch movies releasing in the given month."""
    cached = _get_cached("movie", year, month, page)
    if cached is not None:
        tmdb_page = cached
    else:
        gte, lte = _date_range(year, month)
        with _get_client() as client:
            tmdb_page = client.discover_movies(gte, lte, page)
        _set_cached("movie", year, month, page, tmdb_page)

    results = tmdb_page.get("results") or []

    tmdb_ids = [r["id"] for r in results]
    lcars_matches = _cross_reference(conn, tmdb_ids)

    items = [_flatten_movie(r, lcars_matches.get(r["id"])) for r in results]

    return {
        "items": items,
        "current_page": tmdb_page.get("page", page),
        "total_pages": tmdb_page.get("total_pages", 1),
        "total_results": tmdb_page.get("total_results", len(items)),
        "has_next_page": tmdb_page.get("page", page) < tmdb_page.get(
            "total_pages", 1),
    }


def fetch_tmdb_browse(
    conn: sqlite3.Connection, year: int, month: int,
    media_type: str = "ALL", page: int = 1
) -> dict:
    """Unified browse: TV, MOVIE, or ALL (interleaved by popularity).

    When ``media_type`` is ALL, both TV and movie results for the same
    page are fetched and merged (sorted by popularity descending).
    """
    if media_type == "TV":
        return fetch_tmdb_tv(conn, year, month, page)
    if media_type == "MOVIE":
        return fetch_tmdb_movies(conn, year, month, page)

    # ALL — merge TV + movies for this page
    tv = fetch_tmdb_tv(conn, year, month, page)
    movies = fetch_tmdb_movies(conn, year, month, page)

    merged = tv["items"] + movies["items"]
    merged.sort(key=lambda x: x.get("popularity") or 0, reverse=True)

    return {
        "items": merged,
        "current_page": page,
        "total_pages": max(tv["total_pages"], movies["total_pages"]),
        "total_results": tv["total_results"] + movies["total_results"],
        "has_next_page": tv["has_next_page"] or movies["has_next_page"],
    }
