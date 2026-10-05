#!/usr/bin/env python3
"""Three placements that survive Memory Alpha (user 2026-10-05). Dry run by default.

Memory Alpha rewrites a part's or an id-bearing special level's spans from the AniDB entry on every
pass, so editing those spans does nothing. What it leaves alone is a level of its own:

  - Strawberry 100% S0E6 "Come Pick Me Up!" (abs 12.4) and Sword Art Online S0E23 "Ordinal Scale -
    Sword Art Offline" (abs 51.5) have no AniDB counterpart: each gets a special level of its own
    (the same shape Memory Alpha makes for other unmapped specials), so R1.8 holds.
  - Mushi-shi: the special "Path of Thorns" (abs 37) sits inside part 1's range (27-37): it becomes
    a child of that part, which is how a level inside another's span is held (R1.12).
Rehearsed on a prod copy: all three survive two numbering runs.

    docker exec -i lcars python - [--apply] < place_unplaced_20261005.py
"""

import os
import sqlite3
import sys

from lcars import ids, util

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv

NEW_LEVELS = (  # (show, episode, absolute number, label)
    ("s-5g9yz9", "e-sv73zx", 12.4, "S00E06 Come Pick Me Up! / I`m Always On Your Side"),
    ("s-nnrtx8", "e-42hky7", 51.5,
     "S00E23 Sword Art Online Movie: Ordinal Scale - Sword Art Offline"),
)


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    print("APPLY" if APPLY else "DRY RUN")
    now = util.now_utc_iso()
    with conn:
        for show, episode, number, label in NEW_LEVELS:
            ep = conn.execute("SELECT absolute_number, season_id FROM episode WHERE id = ? AND"
                              " show_id = ?", (episode, show)).fetchone()
            covered = conn.execute(
                "SELECT 1 FROM season z JOIN season_span sp ON sp.season_id = z.id WHERE"
                " z.show_id = ? AND ? BETWEEN sp.abs_from AND sp.abs_to", (show, number)).fetchone()
            ok = ep is not None and ep["absolute_number"] == number and not covered
            print(f"special level for {show} {episode} abs {number}:",
                  "create" if ok else "skipped (not in the audited state)")
            if ok and APPLY:
                zid = ids.generate_id(conn, "z")
                conn.execute(
                    "INSERT INTO season (id, show_id, kind, label, part_number, status, source,"
                    " list_sync, abs_start, abs_end, created_at, updated_at) VALUES (?, ?,"
                    " 'special', ?, 1, 'planned', 'manual', 0, ?, ?, ?, ?)",
                    (zid, show, label, number, number, now, now))
                conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES"
                             " (?, ?, ?)", (zid, number, number))
        row = conn.execute("SELECT parent_id FROM season WHERE id = 'z-b0qn1d' AND kind ="
                           " 'special'").fetchone()
        part = conn.execute("SELECT 1 FROM season WHERE id = 'z-m4pzx9' AND kind = 'part'"
                            ).fetchone()
        print("Mushi-shi special z-b0qn1d under part z-m4pzx9:",
              "set" if row and part and row[0] != "z-m4pzx9" else "skipped (already, or changed)")
        if APPLY and row and part and row[0] != "z-m4pzx9":
            conn.execute("UPDATE season SET parent_id = 'z-m4pzx9', updated_at = ? WHERE id ="
                         " 'z-b0qn1d'", (now,))
        if not APPLY:
            conn.rollback()
    if APPLY:
        print("done; foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())


main()
