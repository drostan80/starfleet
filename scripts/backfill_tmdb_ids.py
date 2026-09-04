#!/usr/bin/env python3
"""One-time backfill: resolve TMDB IDs for tracked shows that have a
TVDB ID in show_external_id but no TMDB entry yet.

Uses TMDB's ``/find/{tvdb_id}?external_source=tvdb_id`` endpoint.
Inserts matches as ``service='tmdb'`` rows in show_external_id.

Usage:
    python3 backfill_tmdb_ids.py --dry-run   # preview (still calls TMDB)
    python3 backfill_tmdb_ids.py --apply     # write
"""

import argparse
import sqlite3
import time

DB_PATH = "/db/lcars.db"

# Read the TMDB API key from the LCARS config file (same place the app reads it).
CONFIG_PATH = "/home/lcars/.config/starfleet/lcars/lcars.ini"

FIND_GAPS = """
    SELECT sei.show_id, sei.external_id AS tvdb_id
    FROM show_external_id sei
    JOIN show sh ON sh.id = sei.show_id
    WHERE sei.service = 'tvdb'
      AND sh.tracked = 1
      AND NOT EXISTS (
          SELECT 1 FROM show_external_id t
          WHERE t.show_id = sei.show_id AND t.service = 'tmdb'
      )
    ORDER BY sei.show_id
"""


def load_api_key():
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    key = cfg.get("lcars", "tmdb_api_key", fallback=None)
    if not key:
        raise SystemExit(f"No tmdb.api_key found in {CONFIG_PATH}")
    return key


def find_tmdb_id(client, api_key, tvdb_id):
    """Call TMDB /find to resolve a TVDB ID → TMDB ID (TV show)."""
    url = f"https://api.themoviedb.org/3/find/{tvdb_id}"
    params = {"api_key": api_key, "external_source": "tvdb_id"}
    resp = client.get(url, params=params)
    if resp.status_code == 429:
        retry_after = float(resp.headers.get("Retry-After", "2"))
        time.sleep(retry_after + 0.5)
        resp = client.get(url, params=params)
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}"
    data = resp.json()
    results = data.get("tv_results") or []
    if results:
        return results[0]["id"], None
    return None, None


def main():
    import httpx

    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    api_key = load_api_key()
    conn = sqlite3.connect(DB_PATH)
    gaps = conn.execute(FIND_GAPS).fetchall()

    if not gaps:
        print("No gaps found — all tracked shows with TVDB IDs already have TMDB entries.")
        return

    print(f"Found {len(gaps)} tracked shows with TVDB IDs but no TMDB ID.")

    client = httpx.Client()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    matched = []
    no_match = []
    errors = []

    for i, (show_id, tvdb_id) in enumerate(gaps, 1):
        tmdb_id, err = find_tmdb_id(client, api_key, tvdb_id)
        if err:
            errors.append((show_id, tvdb_id, err))
            print(f"  [{i}/{len(gaps)}] {show_id} tvdb={tvdb_id} → ERROR: {err}")
        elif tmdb_id:
            matched.append((show_id, str(tmdb_id), tvdb_id))
            if i % 100 == 0 or i == len(gaps):
                print(f"  [{i}/{len(gaps)}] ... {len(matched)} matched so far")
        else:
            no_match.append((show_id, tvdb_id))

        # Gentle throttle — TMDB allows ~40 req/s, stay well under
        time.sleep(0.05)

    print(f"\nResults: {len(matched)} matched, {len(no_match)} no match, {len(errors)} errors")

    if no_match and len(no_match) <= 20:
        print("\nNo TMDB match for:")
        for show_id, tvdb_id in no_match:
            name = conn.execute(
                "SELECT COALESCE(primary_title, title_english, title_romaji)"
                " FROM show WHERE id = ?", (show_id,)
            ).fetchone()
            print(f"  {show_id} tvdb={tvdb_id} ({name[0] if name else '?'})")

    if not matched:
        print("\nNothing to insert.")
        conn.close()
        return

    if args.dry_run:
        print(f"\n(dry-run — would insert {len(matched)} tmdb rows)")
        conn.close()
        return

    for show_id, tmdb_id, _tvdb_id in matched:
        url = f"https://www.themoviedb.org/tv/{tmdb_id}"
        conn.execute(
            "INSERT OR IGNORE INTO show_external_id"
            " (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tmdb', ?, ?, ?)",
            (show_id, tmdb_id, url, now),
        )
    conn.commit()
    print(f"\nInserted {len(matched)} tmdb rows.")

    # Verify
    total_tmdb = conn.execute(
        "SELECT COUNT(*) FROM show_external_id WHERE service = 'tmdb'"
    ).fetchone()[0]
    tracked_no_tmdb = conn.execute(
        "SELECT COUNT(*) FROM show sh"
        " WHERE sh.tracked = 1"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM show_external_id t"
        "     WHERE t.show_id = sh.id AND t.service = 'tmdb'"
        "   )"
    ).fetchone()[0]
    print(
        f"\nPost-backfill: {total_tmdb} total tmdb rows,"
        f" {tracked_no_tmdb} tracked shows still without tmdb ID"
    )

    conn.close()


if __name__ == "__main__":
    main()
