"""Anime seasonal browse — ``browseSeasonalAnime`` query implementation.

Fetches AniList's public seasonal catalog, cross-references against LCARS's
own tracked shows (by ``anilist_id`` on both ``show_external_id`` and
``season`` tables), and returns combined metadata + LCARS status.

When AniList is unreachable (AniListError), falls back to MAL's public
seasonal API — same shape, slightly different fields (no studios/format/
duration from MAL's seasonal response; genre vocabulary differs).
``source`` in the returned dict tells the caller which API served the data.

Module-level cache avoids re-fetching unchanged seasonal catalogs from
AniList on every request. Same justification as ``_last_anilist_call_at``
in anilist_client.py: one shared connection, sync execution model.
"""

import logging
import sqlite3
import time

from lcars import anilist_client, mal_client
from lcars import config as cfg

log = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────────

_cache: dict[tuple[str, int, int], tuple[float, dict]] = {}
_CACHE_TTL_PAST = 86400  # 24 hours for past seasons
_CACHE_TTL_CURRENT = 1800  # 30 min for current season
_CACHE_TTL_UPCOMING = 900  # 15 min for upcoming/future

_SEASONS_ORDER = ["WINTER", "SPRING", "SUMMER", "FALL"]


def _current_season_year() -> tuple[str, int]:
    """Return (season, year) for today."""
    import datetime

    now = datetime.date.today()
    month = now.month
    if month <= 3:
        return "WINTER", now.year
    if month <= 6:
        return "SPRING", now.year
    if month <= 9:
        return "SUMMER", now.year
    return "FALL", now.year


def _cache_ttl(season: str, year: int) -> int:
    cur_season, cur_year = _current_season_year()
    cur_idx = _SEASONS_ORDER.index(cur_season) + cur_year * 4
    req_idx = _SEASONS_ORDER.index(season) + year * 4
    if req_idx < cur_idx:
        return _CACHE_TTL_PAST
    if req_idx == cur_idx:
        return _CACHE_TTL_CURRENT
    return _CACHE_TTL_UPCOMING


def _get_cached(season: str, year: int, page: int) -> dict | None:
    key = (season, year, page)
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, data = entry
    if time.monotonic() - cached_at > _cache_ttl(season, year):
        del _cache[key]
        return None
    return data


def _set_cached(season: str, year: int, page: int, data: dict) -> None:
    _cache[(season, year, page)] = (time.monotonic(), data)


# ── Cross-reference ──────────────────────────────────────────────────


def _cross_reference(
    conn: sqlite3.Connection, anilist_ids: list[int]
) -> dict[int, dict]:
    """Return {anilist_id -> {show_id, status, season_id, season_status}}
    for tracked shows matching the given AniList IDs."""
    if not anilist_ids:
        return {}

    result: dict[int, dict] = {}

    # 1. show_external_id (external_id is TEXT)
    placeholders = ",".join("?" for _ in anilist_ids)
    str_ids = [str(aid) for aid in anilist_ids]
    show_rows = conn.execute(
        f"SELECT sei.external_id, sei.show_id, sh.status, sh.tracked"
        f" FROM show_external_id sei"
        f" JOIN show sh ON sh.id = sei.show_id"
        f" WHERE sei.service = 'anilist' AND sei.external_id IN ({placeholders})",
        str_ids,
    ).fetchall()
    for row in show_rows:
        if not row["tracked"] and row["status"] != "skipped":
            continue  # ignore untracked stubs (but keep skipped tombstones)
        aid = int(row["external_id"])
        result[aid] = {
            "show_id": row["show_id"],
            "status": row["status"],
            "season_id": None,
            "season_status": None,
        }

    # 2. season.anilist_id (INTEGER) — may find season-level matches
    season_rows = conn.execute(
        f"SELECT s.id AS season_id, s.anilist_id, s.show_id, s.status AS season_status,"
        f" sh.status AS show_status, sh.tracked"
        f" FROM season s"
        f" JOIN show sh ON sh.id = s.show_id"
        f" WHERE s.anilist_id IN ({placeholders})",
        anilist_ids,
    ).fetchall()
    for row in season_rows:
        if not row["tracked"] and row["show_status"] != "skipped":
            continue  # ignore untracked stubs (but keep skipped tombstones)
        aid = row["anilist_id"]
        # Prefer season-level match over show-level when available
        if aid not in result or result[aid]["season_id"] is None:
            result[aid] = {
                "show_id": row["show_id"],
                "status": row["show_status"],
                "season_id": row["season_id"],
                "season_status": row["season_status"],
            }

    return result


def _cross_reference_by_mal(
    conn: sqlite3.Connection, mal_ids: list[int]
) -> dict[int, dict]:
    """Return {mal_id -> {show_id, status, season_id, season_status}}
    for tracked shows matching the given MAL IDs.

    Same shape as ``_cross_reference`` but keyed on MAL IDs — used when
    the browse data comes from MAL (AniList fallback)."""
    if not mal_ids:
        return {}

    result: dict[int, dict] = {}

    placeholders = ",".join("?" for _ in mal_ids)
    str_ids = [str(mid) for mid in mal_ids]

    # 1. show_external_id — MAL IDs
    show_rows = conn.execute(
        f"SELECT sei.external_id, sei.show_id, sh.status, sh.tracked"
        f" FROM show_external_id sei"
        f" JOIN show sh ON sh.id = sei.show_id"
        f" WHERE sei.service = 'mal' AND sei.external_id IN ({placeholders})",
        str_ids,
    ).fetchall()
    for row in show_rows:
        if not row["tracked"] and row["status"] != "skipped":
            continue
        mid = int(row["external_id"])
        # MAL IDs can be shared across split entries — first match wins
        # (same convention as AniList's own cross-reference)
        if mid not in result:
            result[mid] = {
                "show_id": row["show_id"],
                "status": row["status"],
                "season_id": None,
                "season_status": None,
            }

    # 2. season_external_id — MAL IDs at season level
    season_rows = conn.execute(
        f"SELECT se.external_id, se.season_id, s.show_id, s.status AS season_status,"
        f" sh.status AS show_status, sh.tracked"
        f" FROM season_external_id se"
        f" JOIN season s ON s.id = se.season_id"
        f" JOIN show sh ON sh.id = s.show_id"
        f" WHERE se.service = 'mal' AND se.external_id IN ({placeholders})",
        str_ids,
    ).fetchall()
    for row in season_rows:
        if not row["tracked"] and row["show_status"] != "skipped":
            continue
        mid = int(row["external_id"])
        if mid not in result or result[mid]["season_id"] is None:
            result[mid] = {
                "show_id": row["show_id"],
                "status": row["show_status"],
                "season_id": row["season_id"],
                "season_status": row["season_status"],
            }

    return result


# ── Flattening ───────────────────────────────────────────────────────


def _format_fuzzy_date(d: dict | None) -> str | None:
    """Convert AniList's ``{year, month, day}`` (any can be null) to
    an ISO-like string or None."""
    if not d or not d.get("year"):
        return None
    parts = [str(d["year"])]
    if d.get("month"):
        parts.append(str(d["month"]).zfill(2))
        if d.get("day"):
            parts.append(str(d["day"]).zfill(2))
    return "-".join(parts)


def _flatten_media(media: dict, lcars_match: dict | None) -> dict:
    """Flatten one AniList media item + optional LCARS match into a
    snake_case dict for ``SeasonalBrowseItem``."""
    return {
        "anilist_id": media["id"],
        "mal_id": media.get("idMal"),
        "title_romaji": (media.get("title") or {}).get("romaji"),
        "title_english": (media.get("title") or {}).get("english"),
        "title_native": (media.get("title") or {}).get("native"),
        "cover_image_url": (media.get("coverImage") or {}).get("large"),
        "description": media.get("description"),
        "genres": media.get("genres") or [],
        "format": media.get("format"),
        "episodes": media.get("episodes"),
        "duration": media.get("duration"),
        "status": media.get("status"),
        "studio_names": [
            n["name"] for n in (media.get("studios") or {}).get("nodes", [])
        ],
        "start_date": _format_fuzzy_date(media.get("startDate")),
        "lcars_show_id": lcars_match["show_id"] if lcars_match else None,
        "lcars_status": lcars_match["status"] if lcars_match else None,
        "lcars_season_id": lcars_match["season_id"] if lcars_match else None,
        "lcars_season_status": (
            lcars_match["season_status"] if lcars_match else None
        ),
    }


_MAL_STATUS_MAP = {
    "currently_airing": "RELEASING",
    "finished_airing": "FINISHED",
    "not_yet_aired": "NOT_YET_RELEASED",
}

_MAL_FORMAT_MAP = {
    "tv": "TV",
    "movie": "MOVIE",
    "ova": "OVA",
    "ona": "ONA",
    "special": "SPECIAL",
    "music": "MUSIC",
}


def _flatten_mal_media(node: dict, lcars_match: dict | None) -> dict:
    """Flatten one MAL anime node into the same snake_case shape as
    ``_flatten_media`` — ``SeasonalBrowseItem``-compatible.

    Differences from the AniList path:
    - ``anilist_id`` is None (MAL doesn't carry AniList IDs)
    - ``duration`` is derived from ``average_episode_duration`` (seconds → minutes)
    - ``studio_names`` from MAL's studios list
    - ``format`` mapped from MAL's ``media_type``
    - ``genres`` uses MAL's vocabulary (includes demographics like "Shounen")
    """
    alt_titles = node.get("alternative_titles") or {}
    main_pic = node.get("main_picture") or {}
    start_date = node.get("start_date")  # already ISO "YYYY-MM-DD" or "YYYY-MM" or "YYYY"

    # Studios: MAL returns [{id, name}, ...]
    studios = node.get("studios") or []
    studio_names = [s["name"] for s in studios if s.get("name")]

    # Duration: MAL gives seconds, we want minutes
    avg_dur = node.get("average_episode_duration")
    duration = avg_dur // 60 if avg_dur and avg_dur > 0 else None

    return {
        "anilist_id": None,
        "mal_id": node.get("id"),
        "title_romaji": node.get("title"),  # MAL's title field IS the romaji
        "title_english": alt_titles.get("en") or None,  # None when no English title
        "title_native": alt_titles.get("ja"),
        "cover_image_url": main_pic.get("large") or main_pic.get("medium"),
        "description": node.get("synopsis"),
        "genres": [g["name"] for g in (node.get("genres") or [])],
        "format": _MAL_FORMAT_MAP.get((node.get("media_type") or "").lower()),
        "episodes": node.get("num_episodes") or None,
        "duration": duration,
        "status": _MAL_STATUS_MAP.get(node.get("status")),
        "studio_names": studio_names,
        "start_date": start_date,
        "lcars_show_id": lcars_match["show_id"] if lcars_match else None,
        "lcars_status": lcars_match["status"] if lcars_match else None,
        "lcars_season_id": lcars_match["season_id"] if lcars_match else None,
        "lcars_season_status": (
            lcars_match["season_status"] if lcars_match else None
        ),
    }


# ── Main entry point ─────────────────────────────────────────────────


def fetch_seasonal_browse(
    conn: sqlite3.Connection, season: str, year: int, page: int = 1
) -> dict:
    """Fetch AniList seasonal page + cross-reference against LCARS.

    Returns a dict matching ``SeasonalBrowseResult`` (snake_case keys
    for Ariadne's ``convert_names_case=True``).

    When AniList is unreachable, falls back to MAL's public seasonal
    API. The ``source`` key in the return dict is ``"ANILIST"`` or
    ``"MAL"`` so the client can display a degraded-service banner.
    MAL results don't carry AniList IDs (``anilist_id`` will be None)
    and are cross-referenced by MAL ID instead.
    """
    # Validate season
    if season not in _SEASONS_ORDER:
        raise ValueError(f"Invalid season: {season}")

    # Check cache — stores the raw AniList page, NOT the built result,
    # so cross-reference runs fresh every time (a show added between
    # requests must show its LCARS status immediately, not stale for
    # up to the TTL).
    cached = _get_cached(season, year, page)
    if cached is not None:
        anilist_page = cached
    else:
        try:
            anilist_page = anilist_client.fetch_seasonal_page(season, year, page)
        except anilist_client.AniListError:
            log.warning(
                "AniList seasonal fetch failed for %s %d page %d, trying MAL",
                season, year, page,
            )
            return _fetch_seasonal_from_mal(conn, season, year, page)
        _set_cached(season, year, page, anilist_page)

    media_list = anilist_page.get("media") or []
    page_info = anilist_page.get("pageInfo") or {}

    # Cross-reference
    anilist_ids = [m["id"] for m in media_list]
    lcars_matches = _cross_reference(conn, anilist_ids)

    # Flatten
    items = [
        _flatten_media(m, lcars_matches.get(m["id"]))
        for m in media_list
    ]

    return {
        "items": items,
        "current_page": page_info.get("currentPage", page),
        "last_page": page_info.get("lastPage", 1),
        "total": page_info.get("total", len(items)),
        "has_next_page": page_info.get("hasNextPage", False),
        "source": "ANILIST",
    }


def _fetch_seasonal_from_mal(
    conn: sqlite3.Connection, season: str, year: int, page: int
) -> dict:
    """MAL fallback for ``fetch_seasonal_browse`` — called when AniList is
    unreachable. Raises ``MALError`` if MAL is also down (the resolver
    converts that to a GraphQLError).

    MAL results are NOT cached: the fallback window should be short, and
    caching would risk stale MAL data lingering past AniList's recovery
    (the primary cache is keyed by ``(season, year, page)`` and stores
    the full result dict including ``source``, so an AniList-sourced cache
    entry naturally replaces a MAL one on the next successful AniList call).
    """
    conf = cfg.get_current()
    mal_client_id = conf.mal_client_id
    if not mal_client_id:
        raise mal_client.MALError("MAL client_id not configured")

    mal_page = mal_client.fetch_seasonal_anime(
        year, season, mal_client_id, page=page
    )

    entries = mal_page.get("data") or []
    nodes = [e["node"] for e in entries if e.get("node")]

    # Cross-reference by MAL ID
    mal_ids = [n["id"] for n in nodes if n.get("id")]
    lcars_matches = _cross_reference_by_mal(conn, mal_ids)

    items = [
        _flatten_mal_media(n, lcars_matches.get(n.get("id")))
        for n in nodes
    ]

    # MAL paging: cursor-based, no total/lastPage — synthesize
    paging = mal_page.get("paging") or {}
    has_next = bool(paging.get("next"))

    return {
        "items": items,
        "current_page": page,
        "last_page": page + (1 if has_next else 0),
        "total": len(items),  # MAL doesn't provide a global total
        "has_next_page": has_next,
        "source": "MAL",
    }
