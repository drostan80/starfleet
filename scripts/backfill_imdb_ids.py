#!/usr/bin/env python3
"""One-time backfill: resolve IMDB IDs for tracked shows that have a
TMDB ID in show_external_id but no IMDB entry yet.

Uses TMDB's ``/tv/{id}/external_ids`` and ``/movie/{id}/external_ids``
endpoints. Inserts matches as ``service='imdb'`` rows in show_external_id.

Usage:
    python3 backfill_imdb_ids.py --dry-run   # preview (still calls TMDB)
    python3 backfill_imdb_ids.py --apply     # write
"""

import argparse
import sqlite3
import time

import httpx

DB_PATH = "/db/lcars.db"
CONFIG_PATH = "/home/lcars/.config/starfleet/lcars/lcars.ini"

FIND_GAPS = """
    SELECT sei.show_id, sei.external_id AS tmdb_id, sh.media_shape
    FROM show_external_id sei
    JOIN show sh ON sh.id = sei.show_id
    WHERE sei.service = 'tmdb'
      AND sh.tracked = 1
      AND NOT EXISTS (
          SELECT 1 FROM show_external_id t
          WHERE t.show_id = sei.show_id AND t.service = 'imdb'
      )
    ORDER BY sei.show_id
"""


def load_api_key():
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    key = cfg.get("lcars", "tmdb_api_key", fallback=None)
    if not key:
        raise SystemExit(f"No tmdb_api_key found in {CONFIG_PATH}")
    return key


def fetch_imdb_id(client, api_key, tmdb_id, media_shape):
    """Call TMDB /tv|movie/{id}/external_ids to get the IMDB ID."""
    media_type = "movie" if media_shape == "movie" else "tv"
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids"
    params = {"api_key": api_key}
    try:
        resp = client.get(url, params=params)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2"))
            time.sleep(retry_after + 0.5)
            resp = client.get(url, params=params)
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        data = resp.json()
        imdb_id = data.get("imdb_id")
        if imdb_id:
            return imdb_id, None
        return None, None
    except Exception as e:
        return None, str(e)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    api_key = load_api_key()
    conn = sqlite3.connect(DB_PATH)
    gaps = conn.execute(FIND_GAPS).fetchall()

    if not gaps:
        print("No gaps found — all tracked shows with TMDB IDs already have IMDB entries.")
        return

    print(f"Found {len(gaps)} tracked shows with TMDB IDs but no IMDB ID.")

    client = httpx.Client()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    matched = []
    no_match = []
    errors = []

    for i, (show_id, tmdb_id, media_shape) in enumerate(gaps, 1):
        imdb_id, err = fetch_imdb_id(client, api_key, tmdb_id, media_shape)
        if err:
            errors.append((show_id, tmdb_id, err))
            print(f"  [{i}/{len(gaps)}] {show_id} tmdb={tmdb_id} → ERROR: {err}")
        elif imdb_id:
            matched.append((show_id, imdb_id, media_shape))
            if i % 100 == 0 or i == len(gaps):
                print(f"  [{i}/{len(gaps)}] ... {len(matched)} matched so far")
        else:
            no_match.append((show_id, tmdb_id))

        # Gentle throttle
        time.sleep(0.05)

    print(f"\nResults: {len(matched)} matched, {len(no_match)} no match, {len(errors)} errors")

    if no_match and len(no_match) <= 30:
        print("\nNo IMDB ID for:")
        for show_id, tmdb_id in no_match:
            name = conn.execute(
                "SELECT COALESCE(primary_title, title_english, title_romaji)"
                " FROM show WHERE id = ?", (show_id,)
            ).fetchone()
            print(f"  {show_id} tmdb={tmdb_id} ({name[0] if name else '?'})")

    if not matched:
        print("\nNothing to insert.")
        conn.close()
        return

    if args.dry_run:
        print(f"\n(dry-run — would insert {len(matched)} imdb rows)")
        conn.close()
        return

    for show_id, imdb_id, media_shape in matched:
        media_type = "movie" if media_shape == "movie" else "tv"
        url = f"https://www.imdb.com/title/{imdb_id}/"
        conn.execute(
            "INSERT OR IGNORE INTO show_external_id"
            " (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'imdb', ?, ?, ?)",
            (show_id, imdb_id, url, now),
        )
    conn.commit()
    print(f"\nInserted {len(matched)} imdb rows.")

    # Verify
    total_imdb = conn.execute(
        "SELECT COUNT(*) FROM show_external_id WHERE service = 'imdb'"
    ).fetchone()[0]
    tracked_no_imdb = conn.execute(
        "SELECT COUNT(*) FROM show sh"
        " WHERE sh.tracked = 1"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM show_external_id t"
        "     WHERE t.show_id = sh.id AND t.service = 'imdb'"
        "   )"
    ).fetchone()[0]
    print(f"\nPost-backfill: {total_imdb} total imdb rows, {tracked_no_imdb} tracked shows still without imdb ID")

    conn.close()


if __name__ == "__main__":
    main()
