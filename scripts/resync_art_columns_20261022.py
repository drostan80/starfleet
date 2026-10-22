#!/usr/bin/env python3
"""One-off (user 10-22): a picked banner / cover reverted on the calendar and other views.

`show.banner_url` / `show.poster_url` are what the calendar, backlog and lists display; they are
meant to mirror the selected show-level art asset. They had drifted (87 banners, 67 posters):
a deselect blanked the column while another asset stayed selected, an already-active card was not
re-selected, and the AniList refresh overwrote the column. v0.4.17 keeps them in line; this repairs
the existing rows with the same function (`art.sync_show_art_columns`): the column becomes the URL
of the selected asset (several selected banners: the one already in the column, else the 'banner'
kind), untouched where nothing is selected. Local only. Dry run unless --apply.

    docker exec -i lcars python - < scripts/resync_art_columns_20261022.py            # list
    docker exec -i lcars python - --apply < scripts/resync_art_columns_20261022.py    # apply

Usage: [--db /db/lcars.db] [--apply]   (take a labelled snapshot first)
"""

import argparse
import sqlite3

from lcars import art

DEFAULT_DB = "/db/lcars.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    changes = []
    for row in conn.execute(
        "SELECT id, COALESCE(title_english, title_romaji) AS title, banner_url, poster_url"
        " FROM show"
    ).fetchall():
        art.sync_show_art_columns(conn, row["id"], clear=False)
        after = conn.execute(
            "SELECT banner_url, poster_url FROM show WHERE id = ?", (row["id"],)).fetchone()
        for column in ("banner_url", "poster_url"):
            if after[column] != row[column]:
                changes.append((row["title"], column, row[column], after[column]))

    banners = sum(1 for c in changes if c[1] == "banner_url")
    to_null = sum(1 for c in changes if c[3] is None)
    print(f"{len(changes)} column(s) to change: {banners} banner, {len(changes) - banners} poster"
          f" ({to_null} to NULL — expected 0)")
    for title, column, old, new in changes[:15]:
        print(f"  {title}: {column} {old and old[-48:]!r} -> {new and new[-48:]!r}")
    if len(changes) > 15:
        print(f"  … and {len(changes) - 15} more")
    if args.apply:
        conn.commit()
        print("applied")
    else:
        conn.rollback()
        print("dry run — nothing written (use --apply)")


if __name__ == "__main__":
    main()
