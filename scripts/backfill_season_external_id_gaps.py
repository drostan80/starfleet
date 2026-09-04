#!/usr/bin/env python3
"""One-time backfill: fill season_external_id rows for seasons that have
anilist_id or mal_id on the season column but no matching
season_external_id entry.

These are pre-S2 dual-write gaps — seasons whose IDs were set before
the dual-write hooks were deployed (v0.1.40).

Usage:
    python3 backfill_season_external_id_gaps.py --dry-run   # preview
    python3 backfill_season_external_id_gaps.py --apply     # write
"""

import argparse
import sqlite3
import sys

DB_PATH = "/db/lcars.db"

FIND_GAPS = """
    SELECT s.id, 'anilist' AS service, CAST(s.anilist_id AS TEXT) AS ext_id
    FROM season s
    WHERE s.anilist_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM season_external_id se
          WHERE se.season_id = s.id AND se.service = 'anilist'
      )
    UNION ALL
    SELECT s.id, 'mal', CAST(s.mal_id AS TEXT)
    FROM season s
    WHERE s.mal_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM season_external_id se
          WHERE se.season_id = s.id AND se.service = 'mal'
      )
"""


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    gaps = conn.execute(FIND_GAPS).fetchall()

    if not gaps:
        print("No gaps found — season_external_id is fully in sync.")
        return

    print(f"Found {len(gaps)} missing season_external_id entries:")
    for season_id, service, ext_id in gaps:
        print(f"  {season_id}  {service}={ext_id}")

    if args.dry_run:
        print("\n(dry-run — no changes written)")
        return

    for season_id, service, ext_id in gaps:
        conn.execute(
            "INSERT OR IGNORE INTO season_external_id"
            " (season_id, service, external_id) VALUES (?, ?, ?)",
            (season_id, service, ext_id),
        )
    conn.commit()
    print(f"\nBackfilled {len(gaps)} entries.")

    # Verify parity
    for svc, col in [("anilist", "anilist_id"), ("mal", "mal_id")]:
        se_count = conn.execute(
            "SELECT COUNT(*) FROM season_external_id WHERE service = ?", (svc,)
        ).fetchone()[0]
        s_count = conn.execute(
            f"SELECT COUNT(*) FROM season WHERE {col} IS NOT NULL"
        ).fetchone()[0]
        status = "✓ EXACT" if se_count == s_count else "✗ MISMATCH"
        print(f"  {svc}: season_external_id={se_count}  season.{col}={s_count}  {status}")

    conn.close()


if __name__ == "__main__":
    main()
