"""Provisional episodes from Syoboi (user 2026-10-05).

TVDB (through Sonarr) is the episode list. For a running anime it can lag: several shows aired two
episodes while TVDB still listed only a placeholder first one, so the second could be neither opened
nor marked watched. Syoboi lists every numbered broadcast (`count` and the exact start time), so
until TVDB takes over, the missing episodes are added from it:

  - anime only, tracked, watching or planned, with at least one real episode;
  - the TV season they belong to is the latest one TVDB has episodes for; its last level (the
    season, or its last part) is matched to Syoboi's broadcasts at episode level by air date
    (syoboi_levels.align_level: no id is trusted to say which TID covers it), and the next numbers
    continue that TID's count;
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


def _runtime(conn, tid: int, count: int) -> int | None:
    row = conn.execute(
        "SELECT st_time_utc, ed_time_utc FROM syoboi_program WHERE tid = ? AND count = ?"
        " AND deleted = 0 AND st_time_utc IS NOT NULL ORDER BY st_time_utc LIMIT 1", (tid, count),
    ).fetchone()
    try:
        a = dt.datetime.strptime(row["st_time_utc"][:19], "%Y-%m-%dT%H:%M:%S")
        b = dt.datetime.strptime(row["ed_time_utc"][:19], "%Y-%m-%dT%H:%M:%S")
        return int((b - a).total_seconds() // 60) or None
    except (TypeError, ValueError):
        return None


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
    season = conn.execute(
        "SELECT MAX(season) FROM episode WHERE show_id = ? AND season > 0 AND kind = 'regular'"
        " AND provisional = 0", (show_id,),
    ).fetchone()[0]
    wanted: set[int] = set()
    shows_ok = show["status"] in ("watching", "planned") and season is not None
    plan = None
    if shows_ok:
        plan = _plan(conn, show_id, season, now)
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


def _plan(conn, show_id: str, season: int, now: str) -> dict | None:
    """{"numbers": [...], "broadcasts": {n: {"first", "minutes"}}} or None when no Syoboi run fits
    the latest season's last level at episode level."""
    from lcars import sonarr_match, syoboi_levels

    if sonarr_match.show_numbering_diverged(conn, show_id):
        return None  # LCARS has subdivided this show: its numbers are not Syoboi's
    levels = [z for z in syoboi_levels.leaf_levels(conn, show_id) if z["season_number"] == season]
    if not levels:
        return None
    from lcars import air_sources

    first_dated = min((e["air_date_utc"] for e in syoboi_levels.level_episodes(conn, levels[-1])
                       if e["air_date_utc"]), default=None)
    if air_sources.starts_before_previous_season_ended(conn, show_id, season, first_dated):
        # R1.6/R1.22a: a season does not start before the one before it ended — the dates that
        # made a Syoboi run "fit" are another season's (ten planned shows, 10-07): not a run
        return None
    alignment, eps, books = syoboi_levels.run_for_level(conn, show_id, levels[-1])
    if alignment is None or not eps:
        return None  # not the same run (a cumulative count, another cour, another season)
    tid = alignment["tid"]
    last_index = len(eps) - 1
    last_count = alignment["start_count"] + last_index - alignment["first_index"]
    top = max(e["episode"] for e in eps)
    run = books[tid]
    ahead = {}
    n, count = top + 1, last_count + 1
    while count in run and n <= top + MAX_BEYOND:
        ahead[n] = {"first": run[count], "minutes": _runtime(conn, tid, count)}
        n, count = n + 1, count + 1
    aired = [k for k, b in ahead.items() if b["first"] <= now]
    limit = max(max(aired, default=0), top) + AHEAD
    cap = _anidb_cap(conn, show_id)
    if cap:
        limit = min(limit, cap)
    numbers = [k for k in sorted(ahead) if k <= limit]
    return {"numbers": numbers, "broadcasts": ahead}


def run_all(conn) -> dict:
    """The Memory Alpha step: every tracked anime that has provisional episodes or a Syoboi run."""
    total = {"shows": 0, "created": 0, "removed": 0, "kept_as_real": 0}
    for row in conn.execute(
        "SELECT sh.id FROM show sh WHERE sh.tracked = 1 AND sh.tracking_space = 'anime' AND ("
        " EXISTS (SELECT 1 FROM show_external_id x"
        "         WHERE x.show_id = sh.id AND x.service = 'syoboi')"
        " OR EXISTS (SELECT 1 FROM season_external_id y JOIN season z ON z.id = y.season_id"
        "            WHERE z.show_id = sh.id AND y.service = 'syoboi')"
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
