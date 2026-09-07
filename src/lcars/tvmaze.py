"""TVmaze API client — TV/movie episode data for Memory Alpha.

Free, no API key.  Rate limit: 20 calls/10s (we pace at 0.5s).
Lookup by TVDB or IMDB ID → TVmaze show → all episodes.
Each show response carries TVDB + IMDB external IDs.
Episode data: airdate (YYYY-MM-DD), airtime, runtime, season/episode.

CC BY-SA 4.0 license.
"""

import logging
import time

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.tvmaze.com"
RATE_LIMIT_SECONDS = 0.55  # slightly above 0.5s to stay safe

_last_api_call: float = 0.0


def _normalize_airstamp(stamp: str | None) -> str | None:
    """Normalize TVmaze airstamp to Z-suffix UTC for consistency.

    TVmaze returns '+00:00'; our air_date_utc uses 'Z'.
    """
    if stamp is None:
        return None
    if stamp.endswith("+00:00"):
        return stamp[:-6] + "Z"
    return stamp


def _rate_limit():
    """Enforce minimum spacing between API calls."""
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_api_call = time.time()


def lookup_show_by_tvdb(
    tvdb_id: str, *, client: httpx.Client | None = None
) -> dict | None:
    """Lookup a TVmaze show by TVDB ID.  Returns the show dict or None."""
    return _lookup_show(f"thetvdb={tvdb_id}", client=client)


def lookup_show_by_imdb(
    imdb_id: str, *, client: httpx.Client | None = None
) -> dict | None:
    """Lookup a TVmaze show by IMDB ID.  Returns the show dict or None."""
    return _lookup_show(f"imdb={imdb_id}", client=client)


def _lookup_show(
    query_param: str, *, client: httpx.Client | None = None
) -> dict | None:
    """Internal: lookup show by query string."""
    _rate_limit()
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        resp = client.get(f"{BASE_URL}/lookup/shows?{query_param}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data is None:
            return None
        return data
    except httpx.HTTPError as e:
        log.warning("TVmaze lookup failed (%s): %s", query_param, e)
        return None
    finally:
        if owns_client:
            client.close()


def fetch_episodes(
    tvmaze_id: int, *, client: httpx.Client | None = None
) -> list[dict] | None:
    """Fetch all episodes for a TVmaze show.  Returns episode list or None."""
    _rate_limit()
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        resp = client.get(f"{BASE_URL}/shows/{tvmaze_id}/episodes")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("TVmaze episodes failed (id=%d): %s", tvmaze_id, e)
        return None
    finally:
        if owns_client:
            client.close()


def ingest_episodes(conn, tvmaze_show_id: int, episodes: list[dict],
                    fetched_at: str) -> int:
    """Write TVmaze episodes into tvmaze_episode table.

    Returns number of rows written.
    """
    count = 0
    for ep in episodes:
        season = ep.get("season")
        number = ep.get("number")
        if season is None or number is None:
            continue  # skip specials with null numbering
        conn.execute(
            """INSERT INTO tvmaze_episode
               (tvmaze_show_id, season, episode, title,
                airdate, airstamp, airtime, runtime_minutes, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (tvmaze_show_id, season, episode)
               DO UPDATE SET
                 title = excluded.title,
                 airdate = excluded.airdate,
                 airstamp = excluded.airstamp,
                 airtime = excluded.airtime,
                 runtime_minutes = excluded.runtime_minutes,
                 fetched_at = excluded.fetched_at""",
            (
                tvmaze_show_id,
                season,
                number,
                ep.get("name"),
                ep.get("airdate"),  # "YYYY-MM-DD" or ""
                _normalize_airstamp(ep.get("airstamp")),  # → "2013-06-25T02:00:00Z"
                ep.get("airtime"),  # "HH:MM" or ""
                ep.get("runtime"),
                fetched_at,
            ),
        )
        count += 1
    conn.commit()
    return count


def drip_fetch_episodes(conn, *, limit: int = 5) -> dict:
    """Fetch TVmaze episodes for up to `limit` TV shows that need them.

    Picks non-anime tracked shows that have a TVDB or IMDB ID but no
    tvmaze_episode rows yet (via show_external_id service='tvmaze').
    When a show has no TVmaze external ID yet, looks it up by TVDB/IMDB,
    stores the TVmaze ID, then fetches episodes.

    Returns stats: {fetched, episodes_stored, skipped, shows_without_tvmaze}.
    """
    import sqlite3

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats = {"fetched": 0, "episodes_stored": 0, "skipped": 0,
             "shows_without_tvmaze": 0}

    # Find non-anime tracked shows that don't have tvmaze_episode data yet.
    # Strategy: shows with a tvmaze external_id but no tvmaze_episode rows,
    # OR shows without a tvmaze external_id (need lookup first).
    #
    # Phase 1: shows that need TVmaze lookup (no tvmaze external_id)
    needs_lookup = conn.execute(
        """SELECT s.id,
                  tvdb.external_id AS tvdb_id,
                  imdb.external_id AS imdb_id
           FROM show s
           LEFT JOIN show_external_id tvdb
             ON tvdb.show_id = s.id AND tvdb.service = 'tvdb'
           LEFT JOIN show_external_id imdb
             ON imdb.show_id = s.id AND imdb.service = 'imdb'
           WHERE s.tracked = 1
             AND s.tracking_space != 'anime'
             AND NOT EXISTS (
               SELECT 1 FROM show_external_id tm
               WHERE tm.show_id = s.id AND tm.service = 'tvmaze'
             )
             AND (tvdb.external_id IS NOT NULL OR imdb.external_id IS NOT NULL)
           LIMIT ?""",
        (limit,),
    ).fetchall()

    # Phase 2: shows that have tvmaze ID but no episodes fetched
    has_id_needs_eps = conn.execute(
        """SELECT s.id, tm.external_id AS tvmaze_id
           FROM show s
           JOIN show_external_id tm
             ON tm.show_id = s.id AND tm.service = 'tvmaze'
           WHERE s.tracked = 1
             AND s.tracking_space != 'anime'
             AND tm.external_id != '-1'
             AND NOT EXISTS (
               SELECT 1 FROM tvmaze_episode te
               WHERE te.tvmaze_show_id = CAST(tm.external_id AS INTEGER)
             )
           LIMIT ?""",
        (limit,),
    ).fetchall()

    # Merge — phase 1 first (establishes IDs), then phase 2
    work_items: list[tuple[str, int | None, str | None, str | None]] = []
    seen = set()

    for show_id, tvdb_id, imdb_id in needs_lookup:
        if show_id not in seen and len(work_items) < limit:
            work_items.append((show_id, None, tvdb_id, imdb_id))
            seen.add(show_id)

    for show_id, tvmaze_id_str in has_id_needs_eps:
        if show_id not in seen and len(work_items) < limit:
            work_items.append((show_id, int(tvmaze_id_str), None, None))
            seen.add(show_id)

    if not work_items:
        return stats

    client = httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        for show_id, tvmaze_id, tvdb_id, imdb_id in work_items:
            # Step 1: resolve TVmaze ID if needed
            if tvmaze_id is None:
                show_data = None
                # Try IMDB first (99% coverage), then TVDB (70%)
                if imdb_id:
                    show_data = lookup_show_by_imdb(imdb_id, client=client)
                if show_data is None and tvdb_id:
                    show_data = lookup_show_by_tvdb(tvdb_id, client=client)

                if show_data is None:
                    stats["shows_without_tvmaze"] += 1
                    # Mark as "not found" so the show doesn't block a slot forever.
                    # external_id='-1' satisfies the NOT EXISTS gate without
                    # colliding with any real tvmaze_show_id.
                    try:
                        conn.execute(
                            """INSERT INTO show_external_id
                               (show_id, service, external_id, url, created_at)
                               SELECT ?, 'tvmaze', '-1', '', ?
                               WHERE NOT EXISTS (
                                 SELECT 1 FROM show_external_id
                                 WHERE show_id = ? AND service = 'tvmaze'
                               )""",
                            (show_id, now, show_id),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        pass
                    continue

                tvmaze_id = show_data["id"]
                # Store TVmaze ID
                try:
                    conn.execute(
                        """INSERT INTO show_external_id
                           (show_id, service, external_id, url, created_at)
                           SELECT ?, 'tvmaze', ?, ?, ?
                           WHERE NOT EXISTS (
                             SELECT 1 FROM show_external_id
                             WHERE show_id = ? AND service = 'tvmaze'
                           )""",
                        (show_id, str(tvmaze_id),
                         f"https://www.tvmaze.com/shows/{tvmaze_id}",
                         now, show_id),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    pass

                # Also backfill TVDB/IMDB from TVmaze if we're missing them
                externals = show_data.get("externals", {})
                _backfill_id(conn, show_id, "tvdb", externals.get("thetvdb"),
                             "https://thetvdb.com/dereferrer/series/{}", now)
                _backfill_id(conn, show_id, "imdb", externals.get("imdb"),
                             "https://www.imdb.com/title/{}/", now)

            # Step 2: fetch episodes
            episodes = fetch_episodes(tvmaze_id, client=client)
            if episodes is None:
                stats["skipped"] += 1
                # Write a tombstone so we don't retry
                _write_tvmaze_tombstone(conn, tvmaze_id, now)
                continue

            if not episodes:
                stats["skipped"] += 1
                _write_tvmaze_tombstone(conn, tvmaze_id, now)
                continue

            count = ingest_episodes(conn, tvmaze_id, episodes, now)
            stats["fetched"] += 1
            stats["episodes_stored"] += count
            log.info("TVmaze drip: show_id=%s, tvmaze=%d → %d episodes",
                     show_id, tvmaze_id, count)
    finally:
        client.close()

    return stats


def _backfill_id(conn, show_id: str, service: str,
                 ext_id, url_template: str, now: str):
    """Insert a missing external ID if we got it from TVmaze."""
    import sqlite3

    if not ext_id:
        return
    ext_id_str = str(ext_id)
    try:
        conn.execute(
            """INSERT INTO show_external_id
               (show_id, service, external_id, url, created_at)
               SELECT ?, ?, ?, ?, ?
               WHERE NOT EXISTS (
                 SELECT 1 FROM show_external_id
                 WHERE show_id = ? AND service = ?
               )""",
            (show_id, service, ext_id_str,
             url_template.format(ext_id_str), now,
             show_id, service),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass


def _write_tvmaze_tombstone(conn, tvmaze_show_id: int, fetched_at: str):
    """Insert a placeholder so NOT EXISTS skips this show."""
    conn.execute(
        """INSERT INTO tvmaze_episode
           (tvmaze_show_id, season, episode, fetched_at)
           VALUES (?, 0, 0, ?)
           ON CONFLICT DO NOTHING""",
        (tvmaze_show_id, fetched_at),
    )
    conn.commit()


def fill_airdate_gaps(conn) -> int:
    """Fill episode.air_date_utc IS NULL from TVmaze episodes.

    Joins on sonarr_season/sonarr_episode (TVDB-compatible numbering)
    via the show's tvmaze external_id.  NULL-only — never overwrites
    existing airdates.  Stamps air_date_source = 'tvmaze'.
    """
    # SQLite UPDATE...FROM: the target table (episode) is implicit in
    # the SET/WHERE clauses; the FROM builds the joined source rows.
    cursor = conn.execute(
        """UPDATE episode SET
             air_date_utc = COALESCE(te.airstamp, te.airdate || 'T00:00:00Z'),
             air_date_source = 'tvmaze'
           FROM show_external_id tm, tvmaze_episode te
           WHERE tm.show_id = episode.show_id
             AND tm.service = 'tvmaze'
             AND te.tvmaze_show_id = CAST(tm.external_id AS INTEGER)
             AND te.season = episode.sonarr_season
             AND te.episode = episode.sonarr_episode
             AND episode.air_date_utc IS NULL
             AND episode.kind = 'regular'
             AND (te.airstamp IS NOT NULL OR (te.airdate IS NOT NULL AND te.airdate != ''))"""
    )
    filled = cursor.rowcount
    if filled:
        conn.commit()
        log.info("Filled %d episode airdate gaps from TVmaze", filled)
    return filled
