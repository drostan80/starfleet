#!/usr/bin/env python3
"""One-off (user 10-07): Sonarr deep links that went stale when a series was renamed.

LCARS stored a show's Sonarr `titleSlug` once, when it linked the show. TVDB renamed some series,
Sonarr's slug changed with them, and the stored link kept pointing at a page that no longer exists
(Magic Repo Man, The Cold Sato-san Is Only Sweet to Me, Beast King War God Dandivine). From v0.4.8
the catalog sweep keeps the link current by itself; this corrects the three that exist now.

For each `sonarr` show_external_id whose show has a TVDB id that Sonarr's catalog also has: when the
slug Sonarr holds differs from the stored one, set the stored id and its URL (the URL keeps its
host and path, only the slug changes). Matched by TVDB id only. Dry run unless --apply. Run inside
the lcars container:

    docker exec -i lcars python - < scripts/fix_stale_sonarr_slugs_20261007.py            # list
    docker exec -i lcars python - --apply < scripts/fix_stale_sonarr_slugs_20261007.py    # apply

Usage: [--db /db/lcars.db] [--apply]   (take a labelled snapshot first)
"""

import argparse
import sqlite3

DEFAULT_DB = "/db/lcars.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from lcars import config, sonarr_client

    cfg = config.load_config()
    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        catalog = client.all_series()
    by_tvdb = {str(s.get("tvdbId")): s for s in catalog if s.get("tvdbId") and s.get("titleSlug")}

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT e.show_id, e.external_id, e.url, t.external_id AS tvdb FROM show_external_id e"
        " JOIN show_external_id t ON t.show_id = e.show_id AND t.service = 'tvdb'"
        " WHERE e.service = 'sonarr'").fetchall()
    stale = []
    for show_id, slug, url, tvdb in rows:
        current = by_tvdb.get(tvdb)
        if current and current["titleSlug"] != slug:
            stale.append((show_id, slug, current["titleSlug"], url))
    print(f"{len(stale)} stale Sonarr link(s)")
    for show_id, old, new, url in stale:
        base = (url or "").rsplit("/", 1)[0] if url else ""
        new_url = f"{base}/{new}" if base else url
        print(f"  {show_id}: {old} -> {new}   ({url} -> {new_url})")
        if args.apply:
            conn.execute(
                "UPDATE show_external_id SET external_id = ?, url = ? WHERE show_id = ?"
                " AND service = 'sonarr'", (new, new_url, show_id))
    if args.apply:
        conn.commit()
        print(f"corrected {len(stale)} link(s)")
    else:
        print("(dry run — nothing written; pass --apply)")


if __name__ == "__main__":
    main()
