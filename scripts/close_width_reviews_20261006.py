#!/usr/bin/env python3
"""Closes the open "season N has X episode(s) in LCARS but AniList media M only covers Y" reviews
(2026-10-06, user: nothing on them can be actioned — the ids are right; a different span, a
mini/special in the plan or an extra date resolves it). The check stopped opening them in the
same release; the count difference is listed by `lcars rulecheck` (R1.11w).

Only an open review whose LATEST message is that finding is closed (a chain that ends in the
schedule-drift finding is left alone).

Usage (labelled snapshot first):
    python3 close_width_reviews_20261006.py                 # dry run (default)
    python3 close_width_reviews_20261006.py --apply
    python3 close_width_reviews_20261006.py --db /path/to/lcars.db
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime

DEFAULT_DB = "/db/lcars.db"
MARK = "likely spans multiple AniList entries"
NOTE = "closed 2026-10-06: a count difference is information, not a review (R1.11w)"


def targets(conn) -> list:
    rows = conn.execute(
        "SELECT id, entity_id, proposed_value_chain FROM pending_review WHERE resolved_at IS NULL"
        " AND entity_type = 'season' AND field = 'anilist_id' AND source = 'anilist'").fetchall()
    return [r for r in rows if MARK in json.loads(r["proposed_value_chain"])[-1]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="close them (default: dry run)")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = targets(conn)
    for r in rows:
        print(f"  {r['id']}  {r['entity_id']}  {json.loads(r['proposed_value_chain'])[-1][:110]}")
    if args.apply:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            for r in rows:
                conn.execute("UPDATE pending_review SET resolved_at = ?, resolution_note = ?"
                             " WHERE id = ? AND resolved_at IS NULL", (now, NOTE, r["id"]))
    print(("CLOSED" if args.apply else "DRY RUN (nothing written)") + f": {len(rows)} review(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
