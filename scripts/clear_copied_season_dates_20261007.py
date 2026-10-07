#!/usr/bin/env python3
"""One-off (user 10-07): announced sequels that carry their first season's premiere date.

Thirteen anime held a TBA placeholder "S2E1" dated exactly like S1E1 (source AniList; the date was
copied from the first season). It made the season look aired, so it was never refreshed, a
completed show marked it watched, and Syoboi's first-season run "fit" it: ten planned shows got six
provisional episodes (S2E2-7) cloned from S1's broadcasts (RULEBOOK R1.6, R1.2g, R1.2f; the guards
are in v0.4.7).

For every tracked anime season N >= 2 whose episode 1 has the same air date as season N-1's
episode 1 and whose real episodes are all untitled/"TBA": clear that date (and the AniList
candidate that carried it) and delete the show's unwatched provisional episodes of that season (a
watched one is kept as an ordinary episode). Local only — nothing is pushed to a list. Dry run
unless --apply.

    docker exec -i lcars python - < scripts/clear_copied_season_dates_20261007.py            # list
    docker exec -i lcars python - --apply < scripts/clear_copied_season_dates_20261007.py    # apply

Usage: [--db /db/lcars.db] [--apply]   (take a labelled snapshot first)
"""

import argparse
import sqlite3

DEFAULT_DB = "/db/lcars.db"
TABLES = ("episode_air_candidate", "air_date_change", "episode_external_id",
          "episode_anidb_mapping", "episode_movie_link")


def columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def find(conn):
    out = []
    for r in conn.execute(
        "SELECT e.id, e.show_id, e.season, e.air_date_utc, e.state,"
        " COALESCE(sh.title_romaji, sh.title_english) AS title, sh.status"
        " FROM episode e JOIN show sh ON sh.id = e.show_id"
        " WHERE sh.tracked = 1 AND sh.tracking_space = 'anime' AND e.kind = 'regular'"
        " AND e.season >= 2 AND e.episode = 1 AND e.provisional = 0 AND e.air_date_utc IS NOT NULL"
        " AND e.air_date_utc = (SELECT p.air_date_utc FROM episode p WHERE p.show_id = e.show_id"
        "   AND p.season = e.season - 1 AND p.episode = 1 AND p.kind = 'regular')"
        " AND NOT EXISTS (SELECT 1 FROM episode x WHERE x.show_id = e.show_id AND x.season ="
        "   e.season AND x.kind = 'regular' AND x.provisional = 0"
        "   AND COALESCE(x.title, '') NOT IN ('', 'TBA'))"
    ):
        out.append(dict(r))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    precision = {"air_precision", "air_local_date", "air_aired_at"} <= columns(conn, "episode")
    found = find(conn)
    print(f"{len(found)} season(s) carry the previous season's first date")
    cleared = removed = kept = 0
    for f in found:
        prov = conn.execute(
            "SELECT id, state FROM episode WHERE show_id = ? AND season = ? AND provisional = 1",
            (f["show_id"], f["season"])).fetchall()
        print(f"  {f['title'][:50]:50} {f['status']:10} S{f['season']}E1 {f['air_date_utc']}"
              f"  state={f['state']}  provisional rows={len(prov)}")
        if not args.apply:
            continue
        sets = ("air_date_utc = NULL, air_date_source = NULL,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        if precision:
            sets += ", air_precision = NULL, air_local_date = NULL, air_aired_at = NULL"
        conn.execute(f"UPDATE episode SET {sets} WHERE id = ?", (f["id"],))
        conn.execute("DELETE FROM episode_air_candidate WHERE episode_id = ?"
                     " AND source = 'anilist' AND air_date_utc = ?", (f["id"], f["air_date_utc"]))
        cleared += 1
        for p in prov:
            if p["state"] == "watched":
                conn.execute("UPDATE episode SET provisional = 0 WHERE id = ?", (p["id"],))
                kept += 1
                continue
            for table in TABLES:
                if columns(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE episode_id = ?", (p["id"],))
            conn.execute("DELETE FROM episode WHERE id = ?", (p["id"],))
            removed += 1
    if args.apply:
        conn.commit()
        print(f"cleared {cleared} date(s); removed {removed} provisional episode(s); "
              f"kept {kept} watched one(s) as ordinary episodes")
    else:
        print("(dry run — nothing written; pass --apply)")


if __name__ == "__main__":
    main()
