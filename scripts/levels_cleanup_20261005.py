#!/usr/bin/env python3
"""Two level clean-ups (user OK 2026-10-05). Dry run by default.

  1. A TVDB season divided into parts holds no list id (R1.10, R1.22). Kusuriya S3 got 195516 back
     on 10-05 14:36 from the weekly season mapping (fixed in v0.4.3): the season's copy goes, the
     part keeps it.
  3. Reviews that no longer hold close: Apothecary Diaries' two (the re-stamp above), R.O.D's
     identity review (S1 now holds 209, as Fribb says) and Kanojo no Tomodachi's air-date review
     (its dates agree since the refresh). From v0.4.3 such reviews close themselves.
  2. Parts are numbered in the order of their spans (R1.10b): Ascendance of a Bookworm S1, Dr. STONE
     S4, SAKAMOTO DAYS Part 2 S1. Only the part numbers change (UNIQUE show/season/part: through
     temporary numbers); ids, statuses and list rows stay.

    docker exec -i lcars python - [--apply] < levels_cleanup_20261005.py
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    print("APPLY" if APPLY else "DRY RUN")
    with conn:
        for p in conn.execute(
            "SELECT p.id, p.show_id, p.season_number, p.anilist_id, p.mal_id FROM season p"
            " WHERE p.kind = 'tvdb_season' AND (p.anilist_id IS NOT NULL OR p.mal_id IS NOT NULL)"
            " AND EXISTS (SELECT 1 FROM season c WHERE c.parent_id = p.id AND c.kind = 'part'"
            "   AND ((c.anilist_id IS NOT NULL AND c.anilist_id = p.anilist_id)"
            "     OR (c.mal_id IS NOT NULL AND c.mal_id = p.mal_id)))"
        ).fetchall():
            print(f"season {p['id']} (S{p['season_number']}) holds AniList {p['anilist_id']} / "
                  f"MAL {p['mal_id']} as well as its part: dropping the season's copy")
            if APPLY:
                conn.execute("UPDATE season SET anilist_id = NULL, mal_id = NULL WHERE id = ?",
                             (p["id"],))
                conn.execute("DELETE FROM season_external_id WHERE season_id = ?"
                             " AND service IN ('anilist', 'mal')", (p["id"],))
        groups: dict[str, list] = {}
        for r in conn.execute(
            "SELECT c.id, c.parent_id, c.part_number, c.show_id, c.season_number,"
            " (SELECT MIN(abs_from) FROM season_span s WHERE s.season_id = c.id) AS start"
            " FROM season c WHERE c.kind = 'part' ORDER BY c.parent_id, c.part_number"
        ):
            groups.setdefault(r["parent_id"], []).append(r)
        for parts in groups.values():
            if any(p["start"] is None for p in parts):
                continue
            wanted = sorted(parts, key=lambda p: (p["start"], p["part_number"]))
            if [p["id"] for p in wanted] == [p["id"] for p in parts]:
                continue
            print(f"S{parts[0]['season_number']} of show {parts[0]['show_id']}: "
                  + ", ".join(f"{p['part_number']}→{n}" for n, p in enumerate(wanted, 1)
                              if p["part_number"] != n))
            if APPLY:
                for offset in (1000, 0):  # temporary numbers first (UNIQUE show/season/part)
                    for n, p in enumerate(wanted, 1):
                        conn.execute("UPDATE season SET part_number = ? WHERE id = ?",
                                     (offset + n, p["id"]))
        for rid, why in (
            ("r-sc19n4", "the season is divided into parts; the parts hold the ids"),
            ("r-nj7qwy", "the season is divided into parts; the parts hold the ids"),
            ("r-bf7y7y", "the season's AniList id now agrees with Fribb (R.O.D swap)"),
            ("r-q4p8e4", "the episodes were refreshed; their dates agree with AniList's schedule"),
        ):
            row = conn.execute("SELECT id FROM pending_review WHERE id = ? AND resolved_at IS NULL",
                               (rid,)).fetchone()
            if row:
                print(f"closing review {rid}: {why}")
                if APPLY:
                    conn.execute("UPDATE pending_review SET resolved_at = ?, resolution_note = ?"
                                 " WHERE id = ?", (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                   why, rid))
        if not APPLY:
            conn.rollback()
    if APPLY:
        print("done; foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())


main()
