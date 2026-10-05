#!/usr/bin/env python3
"""Review batch 10-05: the decisions saved on the review page (user 2026-10-05). Dry run by default.

  - 20 levels: every episode marked watched (R2.7: a completed level has every episode watched);
    the user said they watched all of these specials and films. Watch events dated now.
  - 1 level set completed (R2.15: every episode watched, count confirmed).
  - Magic Repo Man: AniList 202250 moves from the show to its only season (R1.23).
  - Mushi-shi S2: one special level for "Path of Thorns" at 37, the duplicate dropped (R1.12).
Declined by the user: "set planned levels with watched episodes to watching" (4).
NOT done, though confirmed: Strawberry 100% 12.4, SAO 51.5 and Mushi-shi's part 1 range. Memory
Alpha rewrites those spans from the AniDB entry on every pass (numbering._apply_level_spans), so
a hand-edited span is undone by the next pass (rehearsed 10-05): they need a code change.

    docker exec -i lcars python - [--apply] < review_batch_20261005.py
"""

import os
import sqlite3
import sys

from lcars import season_ranges, status_rules, util

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv
MARK_WATCHED = [
    "z-1vd001", "z-5pvrys", "z-6bwys7", "z-973dkg", "z-9z0z0w", "z-atkycd",
    "z-b0se14", "z-c4e6yn", "z-d9yvn9", "z-f5j0pp", "z-k7n195", "z-k93vr3",
    "z-m1qeh6", "z-n1ma2y", "z-rbpeak", "z-sfkh0a", "z-sp2c23", "z-sxrf03",
    "z-xvx5w6", "z-zmz14w",
]
DECLINED_WATCHING = ["z-q91dc4", "z-qtra4v", "z-y782bs", "z-zc03jz"]  # the user kept these planned
SET_COMPLETED = ["z-wcrs75"]


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    print("APPLY" if APPLY else "DRY RUN")
    fx = status_rules.Effects()
    with conn:
        touched = set()
        for zid in MARK_WATCHED:
            z = conn.execute("SELECT * FROM season WHERE id = ?", (zid,)).fetchone()
            eps = [e for e in status_rules.level_episodes(conn, z) if e["state"] != "watched"]
            print(f"mark watched: {zid} ({z['status']}): {len(eps)} episode(s)")
            if APPLY and eps:
                status_rules._mark_watched(conn, z["show_id"], eps, fx)
                touched.add(z["show_id"])
        kept = {z: conn.execute("SELECT status FROM season WHERE id = ?", (z,)).fetchone()[0]
                for z in DECLINED_WATCHING}
        for show_id in sorted(touched):  # R2.14/R2.15 on the levels that share those episodes
            status_rules.after_episodes_changed(conn, show_id, "review-batch-20261005")
        for zid, status in kept.items():  # a declined "set to watching" stays declined
            now = conn.execute("SELECT status FROM season WHERE id = ?", (zid,)).fetchone()[0]
            if now != status:
                print(f"restoring {zid} to {status} (declined)")
                conn.execute("UPDATE season SET status = ? WHERE id = ?", (status, zid))
                conn.execute("DELETE FROM season_status_change WHERE season_id = ? AND"
                             " changed_by = 'review-batch-20261005'", (zid,))
        for zid in SET_COMPLETED:
            print(f"set completed: {zid}")
            if APPLY:
                status_rules.set_level_status(conn, zid, "completed", "review-batch-20261005",
                                              confirmed=True)

        # Magic Repo Man (R1.23)
        z = conn.execute("SELECT id, anilist_id FROM season WHERE id = 'z-zvj6pz'").fetchone()
        x = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = 's-29hyx0'"
                         " AND service = 'anilist'").fetchone()
        print("Magic Repo Man:", dict(z) if z else None, "| show-level:", x and x[0])
        if z and z["anilist_id"] is None and x and x[0] == "202250":
            if APPLY:
                now = util.now_utc_iso()
                conn.execute("UPDATE season SET anilist_id = 202250, updated_at = ? WHERE id = ?",
                             (now, z["id"]))
                season_ranges.upsert_season_external_id(conn, z["id"], 202250, None, now)
                conn.execute("DELETE FROM show_external_id WHERE show_id = 's-29hyx0'"
                             " AND service = 'anilist'")
        else:
            print("  skipped: no longer in the audited state")

        # R1.12: Mushi-shi S2
        keep = conn.execute("SELECT id FROM season WHERE id = 'z-b0qn1d' AND kind = 'special'"
                            ).fetchone()
        dup = conn.execute("SELECT id FROM season WHERE id = 'z-zd0czm' AND kind = 'special'"
                           " AND anilist_id IS NULL").fetchone()
        owns = dup and conn.execute(
            "SELECT 1 FROM episode WHERE season_id = 'z-zd0czm'"
            " UNION SELECT 1 FROM season WHERE parent_id = 'z-zd0czm'").fetchone()
        print("Mushi-shi: keep", bool(keep), "| duplicate", bool(dup),
              "| duplicate in use" if owns else "")
        if keep and dup and not owns:
            if APPLY:
                conn.execute("DELETE FROM season_span WHERE season_id = 'z-b0qn1d'")
                conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES"
                             " ('z-b0qn1d', 37, 37)")
                for table in ("season_external_id", "list_row_lock", "art_asset",
                              "season_air_choice", "season_span", "season_status_change"):
                    conn.execute(f"DELETE FROM {table} WHERE season_id = 'z-zd0czm'")
                conn.execute("DELETE FROM season WHERE id = 'z-zd0czm'")
        else:
            print("  skipped: not in the audited state")
        if not APPLY:
            conn.rollback()
    if APPLY:
        print("done; foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())


main()
