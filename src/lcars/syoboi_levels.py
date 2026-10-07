"""Syoboi's TIDs are per run, matched to episodes by air date (user 2026-10-05).

A Syoboi TID is one broadcast run — a cour or season, like an AniList entry, not like a TVDB show:
Shangri-La Frontier S1 is 6805, S2 7180; Dr. STONE has seven TIDs for its seven levels. The old
single show-level id was the first season's TID on 229 of 263 multi-level shows, so the running
season was never fetched. Two rules replace it:

  1. The id lives on the level (season_external_id, service 'syoboi'), seeded from ARM through the
     level's AniList id — like AniList and MAL (R1.22, R1.23).
  2. Nothing trusts an id to say which episodes a TID covers (a few TIDs span several AniList
     entries). Each *level* is matched to Syoboi's numbered broadcasts at **episode level**: the
     level's first dated episode against the first broadcast of every count of every candidate TID
     (the level's own, its show's other levels', the show-level one) — the closest within
     `ALIGN_DAYS` days wins, and the level's episode i is then count + i. A run that fits no level
     (a cumulative count, another cour) matches nothing and is left alone.

`episode_map` is the one answer — {episode id: (tid, count)} — that the Syoboi schedule choices, the
air-date gap fill and the provisional episodes (provisional_episodes.py) all read.
"""

from __future__ import annotations

import datetime as dt
import logging

from lcars import air_time, util

log = logging.getLogger(__name__)

ALIGN_DAYS = 3  # the level's first dated episode vs a broadcast's first airing


def _utc(value: str) -> dt.datetime:
    return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")


def seed_level_ids(conn, al_to_syoboi: dict[int, int]) -> int:
    """A Syoboi id on every level of a tracked anime that ARM maps through its AniList id. Adds
    only (never replaces a stored id). Returns how many were added."""
    now = util.now_utc_iso()
    added = 0
    for z in conn.execute(
        "SELECT z.id, z.anilist_id FROM season z JOIN show s ON s.id = z.show_id"
        " WHERE s.tracked = 1 AND s.tracking_space = 'anime' AND z.anilist_id IS NOT NULL"
        " AND z.kind IN ('tvdb_season', 'part')"
        " AND NOT EXISTS (SELECT 1 FROM season_external_id x WHERE x.season_id = z.id"
        "                 AND x.service = 'syoboi')"
    ).fetchall():
        tid = al_to_syoboi.get(int(z["anilist_id"]))
        if tid is None:
            continue
        added += conn.execute(
            "INSERT OR IGNORE INTO season_external_id (season_id, service, external_id, url,"
            " created_at) VALUES (?, 'syoboi', ?, ?, ?)",
            (z["id"], str(tid), f"https://cal.syoboi.jp/tid/{tid}", now),
        ).rowcount
    return added


def fetchable_tids(conn) -> list[int]:
    """The level TIDs worth fetching: tracked anime that are watching or planned (the shows whose
    schedules and episodes are still moving). Show-level ids are fetched as before."""
    return sorted({
        int(r[0]) for r in conn.execute(
            "SELECT x.external_id FROM season_external_id x JOIN season z ON z.id = x.season_id"
            " JOIN show s ON s.id = z.show_id WHERE x.service = 'syoboi' AND s.tracked = 1"
            " AND s.tracking_space = 'anime' AND s.status IN ('watching', 'planned')"
        ) if str(r[0]).isdigit()
    })


def leaf_levels(conn, show_id: str) -> list:
    """The levels that hold episodes: TVDB seasons with no parts, and parts, in season order."""
    return conn.execute(
        "SELECT z.* FROM season z WHERE z.show_id = ? AND z.season_number > 0 AND ("
        " z.kind = 'part' OR (z.kind = 'tvdb_season' AND NOT EXISTS"
        "  (SELECT 1 FROM season c WHERE c.parent_id = z.id AND c.kind = 'part')))"
        " ORDER BY z.season_number, z.part_number", (show_id,),
    ).fetchall()


def level_episodes(conn, level) -> list:
    """The level's regular, non-provisional episodes in order."""
    from lcars import status_rules

    ids = [e["id"] for e in status_rules.level_episodes(conn, level)]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT id, season, episode, air_date_utc, absolute_number FROM episode"
        f" WHERE id IN ({marks}) AND kind = 'regular' AND provisional = 0"
        f" ORDER BY absolute_number IS NULL, absolute_number, season, episode", ids,
    ).fetchall()


def broadcasts(conn, tids) -> dict[int, dict[int, str]]:
    """{tid: {count: earliest start UTC}} — the numbered first airings."""
    out: dict[int, dict[int, str]] = {}
    for tid in tids:
        rows = conn.execute(
            "SELECT count, MIN(st_time_utc) AS first FROM syoboi_program WHERE tid = ?"
            " AND deleted = 0 AND count IS NOT NULL AND count > 0 AND st_time_utc IS NOT NULL"
            " GROUP BY count", (tid,),
        ).fetchall()
        if rows:
            out[tid] = {r["count"]: r["first"] for r in rows}
    return out


def candidate_tids(conn, show_id: str, level=None) -> list[int]:
    """Every TID that might cover this show's levels: the level's own first, then the show's other
    levels', then the show-level one."""
    found: list[int] = []

    def add(value):
        if value is not None and str(value).isdigit() and int(value) not in found:
            found.append(int(value))

    if level is not None:
        for r in conn.execute("SELECT external_id FROM season_external_id WHERE season_id = ?"
                              " AND service = 'syoboi'", (level["id"],)):
            add(r[0])
    for r in conn.execute(
        "SELECT x.external_id FROM season_external_id x JOIN season z ON z.id = x.season_id"
        " WHERE z.show_id = ? AND x.service = 'syoboi'"
        " ORDER BY z.season_number DESC, z.part_number DESC", (show_id,),
    ):
        add(r[0])
    for r in conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ?"
                          " AND service = 'syoboi'", (show_id,)):
        add(r[0])
    return found


def align_level(conn, show_id: str, level, eps: list, books: dict) -> dict | None:
    """{"tid", "start_count", "first_index"} — the TID and count that the level's first dated
    episode falls on — or None when no candidate fits within `ALIGN_DAYS` (episode-level
    reconcile: nothing is assumed from ids)."""
    first = next(((i, e) for i, e in enumerate(eps) if e["air_date_utc"]), None)
    if first is None:
        return None
    index, episode = first
    when = _utc(episode["air_date_utc"])
    own = candidate_tids(conn, show_id, level)
    best = None
    for rank, tid in enumerate(own):
        for count, start in books.get(tid, {}).items():
            gap = abs((_utc(start) - when).total_seconds()) / 86400
            if gap > ALIGN_DAYS:
                continue
            key = (round(gap, 3), rank, count)
            if best is None or key < best[0]:
                best = (key, tid, count)
    if best is None:
        return None
    return {"tid": best[1], "start_count": best[2], "first_index": index}


def episode_map(conn, show_id: str) -> dict[str, tuple[int, int]]:
    """{episode id: (tid, count)} for every regular episode of the show that a level's alignment
    reaches. A count that is not in the TID's broadcasts is not mapped."""
    levels = leaf_levels(conn, show_id)
    tids = []
    for level in levels:
        for t in candidate_tids(conn, show_id, level):
            if t not in tids:
                tids.append(t)
    if not tids:
        return {}
    books = broadcasts(conn, tids)
    out: dict[str, tuple[int, int]] = {}
    for level in levels:
        eps = level_episodes(conn, level)
        a = align_level(conn, show_id, level, eps, books)
        if a is None:
            continue
        for i, e in enumerate(eps):
            count = a["start_count"] + i - a["first_index"]
            if count in books[a["tid"]] and e["id"] not in out:
                out[e["id"]] = (a["tid"], count)
    return out


def run_for_level(conn, show_id: str, level, books: dict | None = None):
    """(alignment, eps, books) for one level — what the provisional episodes extend."""
    eps = level_episodes(conn, level)
    tids = candidate_tids(conn, show_id, level)
    books = books if books is not None else broadcasts(conn, tids)
    return align_level(conn, show_id, level, eps, books), eps, books


def fill_gaps(conn) -> int:
    """Fills an episode's empty air date from the earliest Syoboi airing of the (tid, count) its
    level's alignment gives it. NULL-only, source 'syoboi'. Watching/planned tracked anime."""
    filled = 0
    for row in conn.execute(
        "SELECT s.id FROM show s WHERE s.tracked = 1 AND s.tracking_space = 'anime'"
        " AND s.status IN ('watching', 'planned') AND EXISTS (SELECT 1 FROM episode e"
        " WHERE e.show_id = s.id AND e.kind = 'regular' AND e.air_date_utc IS NULL)"
    ).fetchall():
        mapping = episode_map(conn, row["id"])
        if not mapping:
            continue
        books = broadcasts(conn, {t for t, _c in mapping.values()})
        for episode_id, (tid, count) in mapping.items():
            start = books.get(tid, {}).get(count)
            if start:
                filled += conn.execute(
                    "UPDATE episode SET air_date_utc = ?, air_date_source = 'syoboi'"
                    " WHERE id = ? AND air_date_utc IS NULL"
                    f" AND NOT {air_time.LOCKED_SQL}",  # a chosen season has its own rule
                    (start, episode_id),
                ).rowcount
    return filled
