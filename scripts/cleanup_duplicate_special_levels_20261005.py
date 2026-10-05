#!/usr/bin/env python3
"""One-time cleanup: the duplicate `special` levels Memory Alpha made on every pass.

From the cutover (2026-09-30) until the lookup fix (`numbering._special_level`, 2026-10-05) each
pass created a new copy of every special piece that `_nest_in_minis_groups` had moved into its
"Season N minis" group (53 per pass, ~3,450 a day; one piece existed 323 times).

Keeps the OLDEST level of each group of id-less copies (same show, parent and label) and
deletes the others, with their spans and status-history rows. An id-less level beside one that
holds an AniList id is NOT touched: that twin is made once (AniDB data for the piece is not
there yet, so the id-holder is not found) and does not grow. An extra is deleted only if it is a
plain auto-created copy: auto source, no AniList/MAL id, no score/dates, no episode, no child
level, no art, no external
id, no list lock and no chosen schedule. Anything else is left and reported.

Usage (take a labelled snapshot first; stop nothing — it is one short transaction):
    python3 cleanup_duplicate_special_levels_20261005.py                 # dry run (default)
    python3 cleanup_duplicate_special_levels_20261005.py --apply
    python3 cleanup_duplicate_special_levels_20261005.py --db /path/to/lcars.db
"""

import argparse
import sqlite3

DEFAULT_DB = "/db/lcars.db"

# Rows an extra may hold and still be deleted (history of its own creation, and spans).
DELETABLE = {"season_status_change": "season_id"}  # season_span cascades

# Anything else pointing at a level makes it "not a plain copy".
BLOCKING = [
    ("episode", "season_id"),
    ("season", "parent_id"),
    ("art_asset", "season_id"),
    ("season_external_id", "season_id"),
    ("list_row_lock", "season_id"),
    ("season_air_choice", "season_id"),
]


def find_extras(conn):
    """(extra id, kept id, show id, label) for every copy after the oldest of its group."""
    return conn.execute(
        """WITH ranked AS (
             SELECT id, show_id, label,
                    FIRST_VALUE(id) OVER w AS keep_id,
                    ROW_NUMBER() OVER w AS rn
             FROM season
             WHERE kind = 'special' AND source = 'auto'
               AND anilist_id IS NULL AND mal_id IS NULL
             WINDOW w AS (PARTITION BY show_id, IFNULL(parent_id, ''), IFNULL(label, '')
                          ORDER BY created_at, id))
           SELECT id, keep_id, show_id, label FROM ranked WHERE rn > 1"""
    ).fetchall()


def is_plain_copy(conn, season_id: str) -> bool:
    row = conn.execute(
        "SELECT anilist_id, mal_id, score, started_at, completed_at FROM season WHERE id = ?",
        (season_id,),
    ).fetchone()
    if row is None or any(v is not None for v in row):
        return False
    return not any(
        conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (season_id,)).fetchone()
        for table, col in BLOCKING
    )


def run(conn, apply: bool) -> dict:
    conn.execute("PRAGMA foreign_keys = ON")
    fk_before = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    extras = find_extras(conn)
    deletable = [e for e in extras if is_plain_copy(conn, e[0])]
    kept_back = [e for e in extras if e not in deletable]
    result = {
        "extras": len(extras),
        "deletable": len(deletable),
        "left_alone": len(kept_back),
        "groups": len({(e[2], e[1]) for e in extras}),
        "applied": False,
        "seasons_before": conn.execute("SELECT COUNT(*) FROM season").fetchone()[0],
    }
    if apply and deletable:
        with conn:
            for extra_id, _keep, _show, _label in deletable:
                for table, col in DELETABLE.items():
                    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (extra_id,))
                conn.execute("DELETE FROM season WHERE id = ?", (extra_id,))  # spans cascade
        fk_after = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        if fk_after > fk_before:  # problems that were already there are not this script's
            raise SystemExit(
                f"foreign_key_check: {fk_after - fk_before} new problems — restore the snapshot"
            )
        result["applied"] = True
    result["seasons_after"] = conn.execute("SELECT COUNT(*) FROM season").fetchone()[0]
    result["left_alone_examples"] = [(e[0], e[3]) for e in kept_back[:5]]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    result = run(conn, args.apply)
    mode = "APPLIED" if result["applied"] else "DRY RUN (nothing written)"
    print(f"{mode}: {result['extras']} extra levels in {result['groups']} groups;"
          f" {result['deletable']} deletable, {result['left_alone']} left alone;"
          f" seasons {result['seasons_before']} -> {result['seasons_after']}")
    for example in result["left_alone_examples"]:
        print("  left alone:", example)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
