#!/usr/bin/env python3
"""Prod data fixes from the cutover leftovers (user OK 2026-10-05). Dry run by default.

  1. R.O.D -READ OR DIE- (AUDIT-2026-10-05 §3): TVDB S1 (26 episodes) held 208 (the 3-episode OVA)
     and a special level held 209 (the TV series). Swap: S1 <- 209 (planned: your lists have only
     the OVA watched; the 26 "watched" episodes were derived at the cutover and are unwatched
     again, their watch events removed); the OVA's level (its 3 episodes, "Volume 1-3") <- 208,
     completed, its episodes watched.
  2. A1: the season-2 entries held by special levels belong on TVDB S2 itself, the season's only
     entry (R1.10a): Kankin Kuiki Level X 182877 (completed; S2's 6 episodes marked watched),
     Sekai Saikou no Ansatsusha 169579 and Skip and Loafer 185657 (planned, upcoming seasons).

Run inside the lcars container:
    docker exec -i lcars python - [--apply] < leftover_fixes_20261005.py
"""

import os
import sqlite3
import sys

from lcars import level_parts, status_rules, util

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv


def one(conn, sql, *args):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise SystemExit(f"REFUSED: nothing found for {sql} {args}")
    return row


def rod(conn):
    s1 = one(conn, "SELECT * FROM season WHERE id = 'z-b8gy2h'")
    tv = one(conn, "SELECT * FROM season WHERE id = 'z-tj9k0d'")
    ova = one(conn, "SELECT * FROM season WHERE id = 'z-09ke7z'")
    assert (s1["anilist_id"], tv["anilist_id"], ova["anilist_id"]) == (208, 209, None), \
        "R.O.D has changed since the audit — not touching it"
    watched = conn.execute(
        "SELECT COUNT(*) FROM episode WHERE show_id = ? AND season = 1 AND state = 'watched'",
        (s1["show_id"],)).fetchone()[0]
    print(f"R.O.D: S1 {s1['id']} 208 -> 209 planned ({watched} episodes unwatched);"
          f" OVA level {ova['id']} <- 208 completed (3 episodes watched); {tv['id']} goes")
    if not APPLY:
        return
    now = util.now_utc_iso()
    # the OVA's entry (and status, score, dates, list rows, lock) moves to the OVA's own level
    conn.execute(
        "UPDATE season SET anilist_id = ?, mal_id = ?, status = 'completed',"
        " status_set_manually = 1, score = ?, started_at = ?, completed_at = ?, list_sync = ?,"
        " label = COALESCE(label, ?), updated_at = ? WHERE id = ?",
        (s1["anilist_id"], s1["mal_id"], s1["score"], s1["started_at"], s1["completed_at"],
         s1["list_sync"], "R.O.D: Read or Die", now, ova["id"]))
    conn.execute("UPDATE season_external_id SET season_id = ? WHERE season_id = ?"
                 " AND service IN ('anilist', 'mal')", (ova["id"], s1["id"]))
    conn.execute("UPDATE OR IGNORE list_row_lock SET season_id = ? WHERE season_id = ?",
                 (ova["id"], s1["id"]))
    conn.execute("DELETE FROM list_row_lock WHERE season_id = ?", (s1["id"],))
    conn.execute("UPDATE season SET anilist_id = NULL, mal_id = NULL, score = NULL,"
                 " started_at = NULL, completed_at = NULL, status = 'planned',"
                 " status_set_manually = 0, episode_total = NULL WHERE id = ?", (s1["id"],))
    # the TV series' entry (209) goes onto TVDB S1 and its special level goes
    level_parts.link_whole(conn, conn.execute("SELECT * FROM season WHERE id = ?", (s1["id"],)
                                              ).fetchone(),
                           {"anilist_id": 209, "mal_id": 209, "row": tv, "total": 26})
    # the cutover's phantom watches: 26 episodes derived from the OVA's completion
    conn.execute("UPDATE episode SET state = 'unwatched', updated_at = ? WHERE show_id = ?"
                 " AND season = 1", (now, s1["show_id"]))
    conn.execute("DELETE FROM watch_event WHERE show_id = ? AND season = 1", (s1["show_id"],))
    status_rules.set_level_status(conn, ova["id"], "completed", "cleanup-20261005",
                                  confirmed=True)  # R2.7: the OVA's 3 episodes watched
    status_rules.after_episodes_changed(conn, s1["show_id"], "cleanup-20261005")


def season_two(conn, title_like, entry, expect_status):
    special = one(conn, "SELECT z.* FROM season z JOIN show sh ON sh.id = z.show_id"
                  " WHERE z.kind = 'special' AND z.anilist_id = ?", entry)
    parent = one(conn, "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
                 " AND season_number = 2", special["show_id"])
    assert parent["anilist_id"] is None and special["status"] == expect_status, \
        f"{title_like} has changed since the audit — not touching it"
    print(f"{title_like}: AniList {entry} {special['status']} special {special['id']}"
          f" -> TVDB S2 {parent['id']}")
    if not APPLY:
        return None
    level_parts.link_whole(conn, parent, {"anilist_id": special["anilist_id"],
                                          "mal_id": special["mal_id"], "row": special,
                                          "total": None})
    return parent["id"]


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    print("APPLY" if APPLY else "DRY RUN")
    with conn:
        rod(conn)
        season_two(conn, "Sekai Saikou no Ansatsusha", 169579, "planned")
        season_two(conn, "Skip and Loafer", 185657, "planned")
        kankin = season_two(conn, "Kankin Kuiki Level X", 182877, "completed")
        if APPLY and kankin:  # R2.7: a completed level has every episode watched
            status_rules.set_level_status(conn, kankin, "completed", "cleanup-20261005",
                                          confirmed=True)
        if not APPLY:
            conn.rollback()
    if APPLY:
        print("done; foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())


main()
