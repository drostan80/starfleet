"""Provisional episodes from Syoboi (user 2026-10-05).

TVDB (through Sonarr) is the episode list. For a running anime it can lag: several shows aired two
episodes while TVDB still listed only a placeholder first one, so the second could be neither opened
nor marked watched. Syoboi lists every numbered broadcast (`count` and the exact start time), so
until TVDB takes over, the missing episodes are added from it:

  - anime only, tracked, watching or planned, with a Syoboi id and at least one real episode;
  - the TV season they belong to is the latest one TVDB has episodes for, and only when Syoboi's
    numbering fits that season: the first overlapping episode's air date within 3 days of Syoboi's
    first broadcast of the same number (a long runner whose Syoboi count is cumulative fails this
    and is left alone);
  - the numbers after TVDB's last, up to the last one aired plus `AHEAD` (3) more and never more
    than `MAX_BEYOND` (6) past TVDB's last; never beyond the AniDB episode count once AniDB has
    the entry;
  - each is flagged `provisional`, dated by Syoboi's earliest broadcast (source "syoboi"), title
    unknown (shown as TBA).

TVDB takes over: a Sonarr episode with the same number *adopts* the row (`adopt`, called by the
Sonarr sync before it files anything: it gets Sonarr's coordinates and stops being provisional, so
a watched mark survives); a provisional row at or below TVDB's last number that TVDB does not have
is removed (unwatched) or kept as an ordinary row (watched); one beyond what Syoboi/AniDB still
support is removed. Every pass recomputes this.
"""

from __future__ import annotations

import datetime as dt
import logging

from lcars import ids, util

log = logging.getLogger(__name__)

AHEAD = 3  # unaired provisional episodes beyond the last aired one
MAX_BEYOND = 6  # and never more than this many beyond TVDB's last episode
ALIGN_DAYS = 3  # Syoboi's first broadcast of the first shared number vs TVDB's date


def _syoboi_tid(conn, show_id: str) -> int | None:
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'syoboi'",
        (show_id,),
    ).fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else None


def _broadcasts(conn, tid: int) -> dict[int, dict]:
    """{count: {"first": earliest start UTC, "minutes": length}} — Syoboi's numbered broadcasts."""
    out: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT count, st_time_utc, ed_time_utc FROM syoboi_program WHERE tid = ? AND deleted = 0"
        " AND count IS NOT NULL AND count > 0 AND st_time_utc IS NOT NULL ORDER BY st_time_utc",
        (tid,),
    ):
        if r["count"] not in out:
            out[r["count"]] = {"first": r["st_time_utc"], "minutes": _minutes(r)}
    return out


def _minutes(row) -> int | None:
    try:
        a = dt.datetime.strptime(row["st_time_utc"][:19], "%Y-%m-%dT%H:%M:%S")
        b = dt.datetime.strptime(row["ed_time_utc"][:19], "%Y-%m-%dT%H:%M:%S")
        return int((b - a).total_seconds() // 60) or None
    except (TypeError, ValueError):
        return None


def _days_apart(a: str, b: str) -> float:
    fa = dt.datetime.strptime(a[:19], "%Y-%m-%dT%H:%M:%S")
    fb = dt.datetime.strptime(b[:19], "%Y-%m-%dT%H:%M:%S")
    return abs((fa - fb).total_seconds()) / 86400


def _anidb_cap(conn, show_id: str) -> int | None:
    """AniDB's regular episode count, when exactly one AniDB entry belongs to the show and its
    episodes have been fetched."""
    from lcars import anidb

    entries = anidb.anidb_ids_for_show(conn, show_id)
    if len(entries) != 1:
        return None
    row = conn.execute(
        "SELECT MAX(anidb_epno) FROM anidb_episode WHERE anidb_anime_id = ? AND anidb_season = 1",
        (next(iter(entries)),),
    ).fetchone()
    return row[0] if row and row[0] else None


def _delete_episode(conn, episode_id: str) -> None:
    for table in ("episode_air_candidate", "air_date_change", "episode_external_id",
                  "episode_anidb_mapping", "episode_movie_link"):
        conn.execute(f"DELETE FROM {table} WHERE episode_id = ?", (episode_id,))
    conn.execute("DELETE FROM episode WHERE id = ?", (episode_id,))


def adopt(conn, show_id: str, sonarr_episodes: list[dict]) -> int:
    """Called by the Sonarr sync before it files episodes: a provisional row whose season and
    number Sonarr now lists becomes that episode (Sonarr's coordinates, no longer provisional).
    Returns how many were adopted."""
    taken = 0
    for ep in sonarr_episodes:
        if ep.get("seasonNumber") is None or ep.get("episodeNumber") is None:
            continue
        taken += conn.execute(
            "UPDATE episode SET sonarr_season = ?, sonarr_episode = ?, provisional = 0,"
            " air_date_raw_sonarr = COALESCE(air_date_raw_sonarr, ?), updated_at = ?"
            " WHERE show_id = ? AND provisional = 1 AND season = ? AND episode = ?",
            (ep["seasonNumber"], ep["episodeNumber"], ep.get("airDateUtc"), util.now_utc_iso(),
             show_id, ep["seasonNumber"], ep["episodeNumber"]),
        ).rowcount
    return taken


def sync_show(conn, show_id: str, now: str | None = None) -> dict:
    """Creates, keeps and removes this show's provisional episodes. Returns counts."""
    out = {"created": 0, "removed": 0, "kept_as_real": 0}
    now = now or util.now_utc_iso()
    show = conn.execute(
        "SELECT status, tracked, tracking_space FROM show WHERE id = ?", (show_id,)
    ).fetchone()
    if show is None or not show["tracked"] or show["tracking_space"] != "anime":
        return out
    provisional = conn.execute(
        "SELECT id, season, episode, state FROM episode WHERE show_id = ? AND provisional = 1",
        (show_id,),
    ).fetchall()
    tid = _syoboi_tid(conn, show_id)
    season = conn.execute(
        "SELECT MAX(season) FROM episode WHERE show_id = ? AND season > 0 AND kind = 'regular'"
        " AND provisional = 0", (show_id,),
    ).fetchone()[0]
    wanted: set[int] = set()
    shows_ok = (show["status"] in ("watching", "planned") and tid is not None
                and season is not None)
    plan = None
    if shows_ok:
        plan = _plan(conn, show_id, tid, season, now)
        wanted = set(plan["numbers"]) if plan else set()
    last = conn.execute(
        "SELECT MAX(episode) FROM episode WHERE show_id = ? AND season = ? AND provisional = 0"
        " AND kind = 'regular'", (show_id, season),
    ).fetchone()[0] if season is not None else None
    for row in provisional:  # keep, or let TVDB / the sources take over
        superseded = season is not None and row["season"] == season and last is not None \
            and row["episode"] <= last
        unsupported = row["season"] != season or row["episode"] not in wanted
        if not (superseded or unsupported):
            continue
        if row["state"] == "watched":
            conn.execute("UPDATE episode SET provisional = 0 WHERE id = ?", (row["id"],))
            out["kept_as_real"] += 1
        else:
            _delete_episode(conn, row["id"])
            out["removed"] += 1
    if plan:
        have = {r[0] for r in conn.execute(
            "SELECT episode FROM episode WHERE show_id = ? AND season = ?", (show_id, season))}
        season_id = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
            " AND season_number = ?", (show_id, season),
        ).fetchone()
        for n in sorted(wanted - have):
            b = plan["broadcasts"][n]
            conn.execute(
                "INSERT INTO episode (id, show_id, season, season_id, episode, kind, air_date_utc,"
                " air_date_source, runtime_minutes, state, provisional, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'regular', ?, 'syoboi', ?, 'unwatched', 1, ?, ?)",
                (ids.generate_id(conn, "e"), show_id, season,
                 season_id["id"] if season_id else None, n, b["first"], b["minutes"], now, now),
            )
            out["created"] += 1
    return out


def _plan(conn, show_id: str, tid: int, season: int, now: str) -> dict | None:
    """{"numbers": [...], "broadcasts": {...}} or None when Syoboi's numbering doesn't fit."""
    from lcars import sonarr_match

    if sonarr_match.show_numbering_diverged(conn, show_id):
        return None  # LCARS has subdivided this show: its numbers are not Syoboi's
    broadcasts = _broadcasts(conn, tid)
    if not broadcasts:
        return None
    real = conn.execute(
        "SELECT episode, air_date_utc FROM episode WHERE show_id = ? AND season = ?"
        " AND kind = 'regular' AND provisional = 0 ORDER BY episode", (show_id, season),
    ).fetchall()
    if not run_fits(conn, show_id, season, broadcasts):
        return None  # not the same run (a cumulative count, another cour, another season)
    top = max(r["episode"] for r in real)
    aired = [n for n, b in broadcasts.items() if b["first"] <= now]
    limit = min(top + MAX_BEYOND, max(max(aired, default=0), top) + AHEAD)
    cap = _anidb_cap(conn, show_id)
    if cap:
        limit = min(limit, cap)
    numbers = [n for n in sorted(broadcasts) if top < n <= limit]
    return {"numbers": numbers, "broadcasts": broadcasts}


def run_fits(conn, show_id: str, season: int, broadcasts: dict[int, dict]) -> bool:
    """Does Syoboi's numbering fit this TVDB season? The first episode both list has an air date
    within `ALIGN_DAYS` of Syoboi's first broadcast of that number."""
    shared = [
        r for r in conn.execute(
            "SELECT episode, air_date_utc FROM episode WHERE show_id = ? AND season = ?"
            " AND kind = 'regular' AND provisional = 0 ORDER BY episode", (show_id, season),
        ) if r["episode"] in broadcasts and r["air_date_utc"]
    ]
    return bool(shared) and _days_apart(
        shared[0]["air_date_utc"], broadcasts[shared[0]["episode"]]["first"]) <= ALIGN_DAYS


def run_all(conn) -> dict:
    """The Memory Alpha step: every tracked anime that has provisional episodes or a Syoboi run."""
    total = {"shows": 0, "created": 0, "removed": 0, "kept_as_real": 0}
    for row in conn.execute(
        "SELECT sh.id FROM show sh WHERE sh.tracked = 1 AND sh.tracking_space = 'anime' AND ("
        " EXISTS (SELECT 1 FROM show_external_id x"
        "         WHERE x.show_id = sh.id AND x.service = 'syoboi')"
        " OR EXISTS (SELECT 1 FROM episode e WHERE e.show_id = sh.id AND e.provisional = 1))"
    ).fetchall():
        try:
            r = sync_show(conn, row["id"])
        except Exception:
            log.exception("provisional episodes: show=%s failed", row["id"])
            conn.rollback()
            continue
        if any(r.values()):
            total["shows"] += 1
            for k in ("created", "removed", "kept_as_real"):
                total[k] += r[k]
    conn.commit()
    return total
