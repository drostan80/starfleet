#!/usr/bin/env python3
"""Wrong Sonarr deep-link rows (user OK 2026-10-10). Dry run by default.

Until 2026-09-23 the catalog sweep wrote a show's `sonarr` link (the titleSlug behind the Sonarr
icon and deep link) from a fuzzy title match; 35 rows still point at another show's series
(Hamatora -> the-sandman, Preacher -> reacher ...). The TVDB ids were checked and are right; only
this secondary row is wrong. For every tracked episodic show holding a `sonarr` row, by TVDB id:
  - the series IS in Sonarr under that TVDB id -> the row is pointed at its real slug;
  - it is NOT in Sonarr -> the row (and the false "present" marker) is removed.
Nothing else is touched: no tag, no monitoring, no TVDB id.

    docker exec -i lcars python - [--apply] < fix_arr_deep_links_20261010.py
"""

import os
import sqlite3
import sys

from lcars import config, shows, sonarr_client

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv


def main():
    cfg = config.load_config()
    config.set_current(cfg)
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    print("APPLY" if APPLY else "DRY RUN", "| db:", DB)
    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        by_tvdb = {str(s["tvdbId"]): s for s in client.all_series() if s.get("tvdbId")}
    rows = conn.execute(
        "SELECT s.id, coalesce(s.title_english, s.title_romaji) AS title, s.status,"
        " l.external_id AS slug, t.external_id AS tvdb FROM show s"
        " JOIN show_external_id l ON l.show_id = s.id AND l.service = 'sonarr'"
        " JOIN show_external_id t ON t.show_id = s.id AND t.service = 'tvdb'"
        " WHERE s.tracked = 1 AND s.media_shape = 'episodic'").fetchall()
    repoint, remove = [], []
    for r in rows:
        real = by_tvdb.get(str(r["tvdb"]))
        if real is None:
            remove.append(r)
        elif real["titleSlug"] != r["slug"]:
            repoint.append((r, real["titleSlug"]))
    print(f"{len(rows)} sonarr rows: {len(rows) - len(repoint) - len(remove)} already right,"
          f" {len(repoint)} to repoint, {len(remove)} to remove")
    for r, slug in repoint:
        print(f"  repoint  {r['title']} ({r['status']}): {r['slug']} -> {slug}")
    for r in sorted(remove, key=lambda r: r["title"] or ""):
        print(f"  remove   {r['title']} ({r['status']}): {r['slug']}"
              f"  (TVDB {r['tvdb']} is not in Sonarr)")
    if not APPLY:
        print("\nrun with --apply to do it")
        return
    for r, slug in repoint:
        shows.write_arr_external_id(conn, r["id"], "episodic", slug, replace=True)
    for r in remove:
        conn.execute("DELETE FROM show_external_id WHERE show_id = ? AND service = 'sonarr'",
                     (r["id"],))
        conn.execute("UPDATE show_service_presence SET present = 0 WHERE show_id = ? AND"
                     " service = 'sonarr'", (r["id"],))
    conn.commit()
    print(f"\ndone: {len(repoint)} repointed, {len(remove)} removed")


main()
