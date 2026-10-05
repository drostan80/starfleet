"""List-only films become part of their show's numbering (user 2026-10-05, "in doubt, integral").

A film that sits on your AniList/MAL list but that TVDB/Sonarr does not hold has a `special` level
holding its AniList id and no episode — so it has no number and no place in the show (R1.4: a film
that is an integral part of the story takes a whole number between the seasons). This gives each
such level one film episode, so the numbering (`numbering.py`) places it by air date like any other
film and writes the level's span; the level itself is the one that already holds the AniList id and
the list entry — no second level is made.

Which levels: a tracked anime show, a `special` level with an AniList id, not skipped (R2.10), with
no episode, no span (no place in the numbering yet — a film Sonarr or AniDB's mapping already
numbers keeps that) and no child level, whose Fribb entry says it is a MOVIE. The air date and
length come from AniDB (the drip's `anidb_episode` rows, via Fribb's AniDB id) and, when AniDB has nothing for
it, from AniList (one call; a few per pass at most). Without a date the film gets a placeholder
number (R1.0a) until one arrives.

The episode is `kind = 'bonus_movie'` (that is how the numbering knows it is a film whatever its
length — a 35-minute "movie" counts), with no Sonarr coordinates and, when AniDB knows the film,
its AniDB mapping (the numbering must not guess one from the made-up coordinates). A completed
level (R2.7) gets it watched, with a watch event; nothing is pushed anywhere by this module.
"""

import logging

from lcars import anilist_client, fribb, ids, util

log = logging.getLogger(__name__)

# Synthetic season-0 episode numbers start here: clear of TVDB's own specials (S00E01…) so a
# special Sonarr finds later cannot collide with one of ours.
FILM_EPISODE_BASE = 1000
# AniList is asked about at most this many films per pass (only those AniDB has nothing for).
ANILIST_CALLS_PER_PASS = 5


def candidate_levels(conn) -> list:
    return conn.execute(
        "SELECT z.id AS level_id, z.show_id, z.anilist_id, z.status FROM season z"
        " JOIN show sh ON sh.id = z.show_id"
        " WHERE sh.tracked = 1 AND sh.media_shape = 'episodic' AND sh.tracking_space = 'anime'"
        " AND z.kind = 'special' AND z.anilist_id IS NOT NULL"
        " AND z.status IS NOT NULL AND z.status != 'skipped'"
        " AND NOT EXISTS (SELECT 1 FROM episode e WHERE e.season_id = z.id)"
        # no place in the numbering yet: a film TVDB/Sonarr (or AniDB's mapping) already numbers
        # has a span and must not get a second episode
        " AND NOT EXISTS (SELECT 1 FROM season_span sp WHERE sp.season_id = z.id)"
        " AND NOT EXISTS (SELECT 1 FROM season c WHERE c.parent_id = z.id)"
        " ORDER BY z.show_id, z.id"
    ).fetchall()


def _from_anidb(conn, anidb_id: int) -> dict | None:
    row = conn.execute(
        "SELECT airdate, length_minutes FROM anidb_episode"
        " WHERE anidb_anime_id = ? AND anidb_season = 1 AND anidb_epno = 1",
        (anidb_id,),
    ).fetchone()
    if row is None or not row["airdate"]:
        return None
    title = conn.execute(
        "SELECT main_title FROM anidb_anime WHERE anidb_id = ?", (anidb_id,)
    ).fetchone()
    return {"air": f"{row['airdate']}T00:00:00Z", "runtime": row["length_minutes"],
            "title": title["main_title"] if title else None, "source": "anidb"}


def _from_anilist(anilist_id: int) -> dict | None:
    """None when AniList cannot be reached or does not know the id (retried on a later pass)."""
    try:
        media = anilist_client.fetch_media_facts(anilist_id)
    except anilist_client.AniListError:
        log.warning("film_levels: AniList lookup failed for %s", anilist_id)
        return None
    if not media:
        return None
    start = media.get("startDate") or {}
    air = None
    if start.get("year"):
        air = f"{start['year']:04d}-{(start.get('month') or 1):02d}-{(start.get('day') or 1):02d}"
        air += "T00:00:00Z"
    title = media.get("title") or {}
    return {"air": air, "runtime": media.get("duration"),
            "title": title.get("english") or title.get("romaji"), "source": "anilist"}


def create_film_episodes(conn, dataset=None) -> dict:
    """One film episode for each list-only film level. Idempotent: a level that has its episode
    is no longer a candidate. Returns {"created": n, "waiting": n}."""
    result = {"created": 0, "waiting": 0}
    levels = candidate_levels(conn)
    if not levels:
        return result
    try:
        dataset = dataset if dataset is not None else fribb.load_dataset()
    except Exception:
        log.warning("film_levels: no Fribb dataset yet — films wait for the next pass")
        return result
    # Our own index: `fribb.build_anilist_index` leaves out entries with no TVDB id, and a film
    # TVDB does not hold is exactly what this is for.
    index: dict[int, list[dict]] = {}
    for entry in dataset:
        anilist_id = entry.get("anilist_id")
        if isinstance(anilist_id, int) and anilist_id > 0:
            index.setdefault(anilist_id, []).append(entry)
    anilist_calls = 0
    for level in levels:
        entries = index.get(int(level["anilist_id"]), [])
        if not any(e.get("type") == "MOVIE" for e in entries):
            continue  # not a film (an OVA, a special, a TV season…): list-only, nothing to place
        facts = None
        for entry in entries:
            anidb_id = entry.get("anidb_id")
            if isinstance(anidb_id, int) and anidb_id > 0:
                facts = _from_anidb(conn, anidb_id)
                if facts:
                    facts["anidb_id"] = anidb_id
                    break
        if facts is None and anilist_calls < ANILIST_CALLS_PER_PASS:
            anilist_calls += 1
            facts = _from_anilist(int(level["anilist_id"]))
        if facts is None:
            result["waiting"] += 1
            continue
        _create(conn, level, facts)
        result["created"] += 1
    if result["created"]:
        conn.commit()
        log.info("film_levels: %d film episode(s) created", result["created"])
    return result


def _create(conn, level, facts: dict) -> None:
    now = util.now_utc_iso()
    number = max(
        FILM_EPISODE_BASE,
        conn.execute(
            "SELECT COALESCE(MAX(episode), 0) FROM episode WHERE show_id = ? AND season = 0",
            (level["show_id"],),
        ).fetchone()[0] + 1,
    )
    watched = level["status"] == "completed"  # R2.7: a completed level has every episode watched
    episode_id = ids.generate_id(conn, "e")
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, air_date_source,"
        " runtime_minutes, title, state, season_id, created_at, updated_at)"
        " VALUES (?, ?, 0, ?, 'bonus_movie', ?, ?, ?, ?, ?, ?, ?, ?)",
        (episode_id, level["show_id"], number, facts["air"],
         facts["source"] if facts["air"] else None, facts["runtime"], facts["title"],
         "watched" if watched else "unwatched", level["level_id"], now, now),
    )
    if facts.get("anidb_id"):  # its AniDB entry, so the numbering routes it to this level
        conn.execute(
            "INSERT INTO episode_anidb_mapping (episode_id, anidb_anime_id, anidb_season,"
            " anidb_epno, confidence, created_at) VALUES (?, ?, 1, 1, 'auto', ?)",
            (episode_id, facts["anidb_id"], now),
        )
    if watched:
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES (?, ?, 0, ?, ?, ?)",
            (ids.generate_id(conn, "w"), level["show_id"], number, now, now),
        )
