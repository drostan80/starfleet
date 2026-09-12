"""Season absolute-episode range helpers — Slice 2, 2026-08-27.

Two responsibilities, both inert until S3 switches reconcile reads:

1. **Dual-write `season_external_id`**: called from the three sites
   that write `season.anilist_id` / `season.mal_id` (season_mapping,
   metadata._upsert_season, resolvers.setSeasonMapping) so the mapping
   table stays current going forward — S3 won't need its own backfill.

2. **Lazy range-fill**: after `_synthesize_absolute_numbers` in
   `_fetch_sonarr`/`_fetch_sonarr_multi_show`, fills `abs_start`/
   `abs_end` from observed integer `absolute_number` values for any
   season of the show that has episodes but no range yet. This is
   D5's "lazy for the long tail" — the one-time backfill script
   handles the bulk, this catches everything on its next Sonarr touch.

3. **Subdivision width check** (S5, 2026-08-27): `check_subdivision_widths`
   is the `pollSeasonSubdivision` mutation's own implementation. For
   every season that has an abs range + an AniList link, it batch-fetches
   AniList episode counts and opens/extends a `pending_review` row
   (field `season_subdivision`) when range width ≠ AniList's count.
   Skips airing seasons (AniList returns null episodes) and seasons whose
   AniList id isn't in the response at all (dead/wrong link — a separate
   concern). Idempotent: `pending_review.open_or_extend` extends an
   existing open row rather than opening a duplicate, and
   `pending_review.already_resolved_with` suppresses re-opening when a
   human just resolved the same mismatch value.
"""

import logging
import sqlite3

from lcars import anilist_client, ids, pending_review, util

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Width-check internals (ported from scripts/backfill_season_ranges.py so
# the same logic is reachable from the live server, not only the one-time
# script).  The script now imports these instead of keeping its own copy.
# ---------------------------------------------------------------------------

_BATCH = 50
_ANILIST_EPISODES_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) { id episodes }
  }
}
"""


def _fetch_anilist_episode_counts(anilist_ids: list[int]) -> dict[int, int | None]:
    """{anilist_id -> episodes} in batches of 50.

    A missing key means AniList returned no `media` row for that id —
    dead or wrong link, distinct from `episodes: null` (still airing).
    """
    result: dict[int, int | None] = {}
    for start in range(0, len(anilist_ids), _BATCH):
        batch = anilist_ids[start : start + _BATCH]
        data = anilist_client._graphql_request(
            _ANILIST_EPISODES_QUERY, {"ids": batch}, token=None, client=None
        )
        for media in data["Page"]["media"]:
            result[media["id"]] = media.get("episodes")
    return result


def upsert_season_external_id(
    conn: sqlite3.Connection,
    season_id: str,
    anilist_id: int | None,
    mal_id: int | None,
    now: str,
    *,
    anilist_name: str | None = None,
    mal_name: str | None = None,
) -> None:
    """Mirror anilist_id/mal_id into season_external_id.

    Uses INSERT ... ON CONFLICT so it's idempotent — safe to call on
    every write, not just first-time. Deletes a mapping row if the
    caller sets the id to NULL (the season was unlinked).

    Optional name parameters carry the source database's own title for
    this season/entry (e.g. AniList's media title). Name is only written
    if provided and non-None — existing names are preserved on a plain
    ID-only upsert.
    """
    names = {"anilist": anilist_name, "mal": mal_name}
    for service, ext_id in [("anilist", anilist_id), ("mal", mal_id)]:
        if ext_id is not None:
            name = names.get(service)
            if name is not None:
                conn.execute(
                    "INSERT INTO season_external_id"
                    " (season_id, service, external_id, name, created_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (season_id, service)"
                    " DO UPDATE SET external_id = excluded.external_id,"
                    " name = excluded.name",
                    (season_id, service, str(ext_id), name, now),
                )
            else:
                conn.execute(
                    "INSERT INTO season_external_id"
                    " (season_id, service, external_id, created_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (season_id, service)"
                    " DO UPDATE SET external_id = excluded.external_id",
                    (season_id, service, str(ext_id), now),
                )
        else:
            # If the id was cleared, remove the mapping row if it exists
            conn.execute(
                "DELETE FROM season_external_id"
                " WHERE season_id = ? AND service = ?",
                (season_id, service),
            )


def fill_season_ranges(conn: sqlite3.Connection, show_id: str) -> None:
    """Lazy range-fill: for each season of this show that has episodes with
    integer absolute_number values but no range set yet, derive abs_start/
    abs_end from MIN/MAX(absolute_number).

    Skips season 0 (specials). Only writes seasons with no existing range
    — never overwrites a range the backfill script or a human already set.
    The commit is the caller's responsibility (both _fetch_sonarr call
    sites commit after their own episode writes).
    """
    # Only anime shows — TV shows have no meaningful absolute numbering
    # model, and filling ranges for them would write anime-model data
    # for shows the backfill script deliberately skips (same class as
    # the tracking_space check in season_mapping.py:41-48).
    is_anime = conn.execute(
        "SELECT tracking_space FROM show WHERE id = ?", (show_id,)
    ).fetchone()
    if is_anime is None or is_anime["tracking_space"] != "anime":
        return

    seasons = conn.execute(
        "SELECT id, season_number FROM season"
        " WHERE show_id = ? AND season_number > 0"
        "   AND abs_start IS NULL AND abs_end IS NULL",
        (show_id,),
    ).fetchall()
    if not seasons:
        return

    now = util.now_utc_iso()
    for season in seasons:
        range_row = conn.execute(
            "SELECT MIN(absolute_number) AS abs_min,"
            "       MAX(absolute_number) AS abs_max"
            " FROM episode"
            " WHERE show_id = ? AND season = ?"
            "   AND absolute_number IS NOT NULL"
            "   AND absolute_number = CAST(absolute_number AS INTEGER)",
            (show_id, season["season_number"]),
        ).fetchone()
        if range_row["abs_min"] is None:
            continue  # no integer absolute numbers — can't derive a range
        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ?"
            " WHERE id = ?",
            (int(range_row["abs_min"]), int(range_row["abs_max"]), now, season["id"]),
        )


def check_subdivision_widths(conn: sqlite3.Connection) -> dict[str, int]:
    """S5 — the `pollSeasonSubdivision` mutation's implementation.

    For every season that has both an abs range and an AniList link (via
    `season_external_id`), batch-fetch AniList's `episodes` count and
    compare to the season's range width (`abs_end - abs_start + 1`).

    Opens/extends a `pending_review` row (entity_type='season',
    field='season_subdivision') for each mismatch.  Idempotent:
    `pending_review.open_or_extend` extends an existing open row rather
    than opening a duplicate; `pending_review.already_resolved_with`
    suppresses re-opening when a human resolved the same mismatch value.

    Skips:
    - Airing seasons (AniList returns ``episodes: null``).
    - Seasons whose AniList id isn't in the response (dead/wrong link —
      `pending_review` field ``anilist_dead_link`` is a separate concern).

    Returns ``{"checked": int, "flagged": int}``.
    """
    rows = conn.execute(
        "SELECT s.id, s.abs_start, s.abs_end, sex.external_id AS anilist_id"
        " FROM season s"
        " JOIN season_external_id sex"
        "   ON sex.season_id = s.id AND sex.service = 'anilist'"
        " WHERE s.abs_start IS NOT NULL AND s.abs_end IS NOT NULL",
    ).fetchall()
    if not rows:
        return {"checked": 0, "flagged": 0}

    unique_ids = sorted({int(r["anilist_id"]) for r in rows})
    anilist_counts = _fetch_anilist_episode_counts(unique_ids)

    checked = 0
    flagged = 0
    for r in rows:
        anilist_eps = anilist_counts.get(int(r["anilist_id"]))
        if anilist_eps is None:
            # null (airing) or not returned by AniList — skip both
            continue
        checked += 1
        range_width = r["abs_end"] - r["abs_start"] + 1
        if range_width == anilist_eps:
            continue
        # Mismatch — flag if not already resolved with this exact value
        mismatch_str = f"anilist={anilist_eps},range_width={range_width}"
        if pending_review.already_resolved_with(
            conn, "season", r["id"], "season_subdivision", mismatch_str
        ):
            continue
        pending_review.open_or_extend(
            conn,
            "season",
            r["id"],
            "season_subdivision",
            "anilist_width_check",
            None,
            mismatch_str,
        )
        flagged += 1
    conn.commit()
    return {"checked": checked, "flagged": flagged}


# ---------------------------------------------------------------------------
# Step 2 — Season row creation + episode.season_id backfill
# ---------------------------------------------------------------------------


def ensure_all_season_rows(conn: sqlite3.Connection) -> int:
    """Create missing ``season`` rows from ``episode.season`` values.

    For every (show_id, season) pair in the episode table where
    season > 0 and no matching season row exists, inserts a new season
    row.  Anime shows go through ``season_mapping.reconcile_season()``
    (which tries Fribb matching and opens pending_review on failure);
    TV shows get a direct insert with source='unmatched', matched=0
    (no Fribb attempt, no review noise — same path reconcile_season
    already takes for non-anime, but batched).

    Idempotent — safe to call every tick.  Returns the number of
    season rows created.
    """
    from lcars import season_mapping

    # Find all (show_id, season) pairs that have episodes but no season row.
    # Skip season 0 (specials — no cross-service identity).
    gaps = conn.execute(
        "SELECT DISTINCT e.show_id, e.season AS season_number,"
        "  s.tracking_space"
        " FROM episode e"
        " JOIN show s ON s.id = e.show_id"
        " WHERE e.season > 0"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM season se"
        "     WHERE se.show_id = e.show_id"
        "       AND se.season_number = e.season"
        "   )"
        " ORDER BY e.show_id, e.season",
    ).fetchall()

    if not gaps:
        return 0

    created = 0
    now = util.now_utc_iso()
    for row in gaps:
        show_id = row["show_id"]
        season_number = row["season_number"]
        tracking_space = row["tracking_space"]

        if tracking_space == "anime":
            # Full reconcile — Fribb match attempt + pending_review
            try:
                season_mapping.reconcile_season(conn, show_id, season_number)
            except Exception:
                # reconcile_season may have already committed the row before
                # raising (e.g. INSERT succeeded but pending_review failed).
                # Guard against double-insert hitting the UNIQUE constraint.
                exists = conn.execute(
                    "SELECT 1 FROM season"
                    " WHERE show_id = ? AND season_number = ?",
                    (show_id, season_number),
                ).fetchone()
                if not exists:
                    season_id = ids.generate_id(conn, "z")
                    conn.execute(
                        "INSERT INTO season"
                        " (id, show_id, season_number, status, source, matched,"
                        "  manual_override, created_at, updated_at)"
                        " VALUES (?, ?, ?, 'planned', 'unmatched', 0, 0, ?, ?)",
                        (season_id, show_id, season_number, now, now),
                    )
                    conn.commit()
        else:
            # TV/movies — direct creation, no Fribb, no pending_review
            season_id = ids.generate_id(conn, "z")
            conn.execute(
                "INSERT INTO season"
                " (id, show_id, season_number, status, source, matched,"
                "  manual_override, created_at, updated_at)"
                " VALUES (?, ?, ?, 'planned', 'unmatched', 0, 0, ?, ?)",
                (season_id, show_id, season_number, now, now),
            )
            conn.commit()

        created += 1

    if created:
        log.info("ensure_all_season_rows: created %d season rows", created)
    return created


def backfill_episode_season_id(conn: sqlite3.Connection) -> int:
    """Set ``episode.season_id`` for episodes that have a season number
    and a matching ``season`` row but no ``season_id`` yet.

    Strictly NULL-only — never overwrites an existing season_id.
    Skips season 0 (specials have no season row by design).
    Idempotent — safe to call every tick.  Returns the count updated.

    This is the episode-first source of truth: once set, season_id is
    authoritative and is never silently recomputed by this function.
    """
    # When subdivisions exist (part_number > 1), default to the
    # primary part (part_number = 1).  The episode-level cross-database
    # identity (episode_external_id) is what carries the per-source
    # season coordinates; season_id here is the grouping convenience.
    updated = conn.execute(
        "UPDATE episode SET season_id = ("
        "  SELECT se.id FROM season se"
        "  WHERE se.show_id = episode.show_id"
        "    AND se.season_number = episode.season"
        "  ORDER BY se.part_number ASC LIMIT 1"
        ")"
        " WHERE season_id IS NULL"
        "   AND season > 0"
        "   AND EXISTS ("
        "     SELECT 1 FROM season se"
        "     WHERE se.show_id = episode.show_id"
        "       AND se.season_number = episode.season"
        "   )",
    ).rowcount
    if updated:
        conn.commit()
        log.info("backfill_episode_season_id: linked %d episodes", updated)
    return updated


# ---------------------------------------------------------------------------
# Step 3 — Seed episode_external_id
# ---------------------------------------------------------------------------


def seed_episode_external_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Populate ``episode_external_id`` from existing source data.

    Four services seeded:

    - **anidb**: from ``episode_anidb_mapping`` — real per-episode IDs.
      ``external_id`` is ``'{anidb_anime_id}:{anidb_epno}'``,
      ``season_number`` is ``anidb_season``, ``episode_number`` is
      ``anidb_epno``.
    - **tvdb**: from ``episode.sonarr_season``/``sonarr_episode`` (Sonarr's
      TVDB coordinates). ``external_id`` is ``'{sonarr_season}:{sonarr_episode}'``,
      ``season_number`` / ``episode_number`` carry the raw values.
    - **anilist**: from ``season_external_id(service='anilist')`` + the
      episode's position within its season.  ``external_id`` is
      ``'{anilist_id}:{episode_in_season}'``.
    - **mal**: same shape as anilist, from ``season_external_id(service='mal')``.

    Strictly insert-only — skips any (episode_id, service) pair that
    already exists.  Idempotent and safe to call every tick.

    Returns ``{"anidb": n, "tvdb": n, "anilist": n, "mal": n}``.
    """
    now = util.now_utc_iso()
    result = {"anidb": 0, "tvdb": 0, "anilist": 0, "mal": 0}

    # ── AniDB ──
    result["anidb"] = conn.execute(
        "INSERT OR IGNORE INTO episode_external_id"
        " (episode_id, service, external_id, season_number, episode_number, created_at)"
        " SELECT eam.episode_id, 'anidb',"
        "   CAST(eam.anidb_anime_id AS TEXT) || ':' || CAST(eam.anidb_epno AS TEXT),"
        "   eam.anidb_season, eam.anidb_epno, ?"
        " FROM episode_anidb_mapping eam"
        " WHERE eam.anidb_epno IS NOT NULL"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM episode_external_id eid"
        "     WHERE eid.episode_id = eam.episode_id AND eid.service = 'anidb'"
        "   )",
        (now,),
    ).rowcount
    if result["anidb"]:
        conn.commit()

    # ── TVDB (from Sonarr coordinates) ──
    result["tvdb"] = conn.execute(
        "INSERT OR IGNORE INTO episode_external_id"
        " (episode_id, service, external_id, season_number, episode_number, created_at)"
        " SELECT e.id, 'tvdb',"
        "   CAST(e.sonarr_season AS TEXT) || ':' || CAST(e.sonarr_episode AS TEXT),"
        "   e.sonarr_season, e.sonarr_episode, ?"
        " FROM episode e"
        " WHERE e.sonarr_season IS NOT NULL AND e.sonarr_episode IS NOT NULL"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM episode_external_id eid"
        "     WHERE eid.episode_id = e.id AND eid.service = 'tvdb'"
        "   )",
        (now,),
    ).rowcount
    if result["tvdb"]:
        conn.commit()

    # ── AniList / MAL (from season_external_id + episode position) ──
    for service in ("anilist", "mal"):
        # For each episode that has a season_id linked to a
        # season_external_id row for this service, compute the episode's
        # position within its season using ROW_NUMBER ordered by episode
        # number.  The external_id is '{entry_id}:{ep_in_season}'.
        result[service] = conn.execute(
            "INSERT OR IGNORE INTO episode_external_id"
            " (episode_id, service, external_id, season_number, episode_number, created_at)"
            " SELECT sub.episode_id, ?,"
            "   sub.ext_id || ':' || CAST(sub.ep_in_season AS TEXT),"
            "   sub.season_number, sub.ep_in_season, ?"
            " FROM ("
            "   SELECT e.id AS episode_id,"
            "     sei.external_id AS ext_id,"
            "     e.season AS season_number,"
            "     ROW_NUMBER() OVER ("
            "       PARTITION BY e.season_id ORDER BY e.episode"
            "     ) AS ep_in_season"
            "   FROM episode e"
            "   JOIN season_external_id sei"
            "     ON sei.season_id = e.season_id AND sei.service = ?"
            "   WHERE e.season_id IS NOT NULL AND e.season > 0"
            " ) sub"
            " WHERE NOT EXISTS ("
            "   SELECT 1 FROM episode_external_id eid"
            "   WHERE eid.episode_id = sub.episode_id AND eid.service = ?"
            " )",
            (service, now, service, service),
        ).rowcount
        if result[service]:
            conn.commit()

    total = sum(result.values())
    if total:
        log.info(
            "seed_episode_external_ids: anidb=%d tvdb=%d anilist=%d mal=%d",
            result["anidb"], result["tvdb"], result["anilist"], result["mal"],
        )
    return result


# ---------------------------------------------------------------------------
# Step 4 — Backfill season_external_id.name
# ---------------------------------------------------------------------------


def backfill_season_names(conn: sqlite3.Connection) -> int:
    """Fill NULL ``season_external_id.name`` from local title data.

    For **AniList/MAL seasons of anime shows**: derives the AniDB anime ID
    for each season via ``episode_anidb_mapping``, then looks up the
    English official title from ``anidb_title``.  Falls back to romaji
    (``x-jat``) if no English title exists.

    For **single-season shows**: uses the show's own ``primary_title``
    as the season name (it IS the entry title).

    Strictly NULL-only on the name column — never overwrites an existing
    name.  Idempotent and safe to call every tick.

    Returns the number of names filled.
    """
    filled = 0

    # Collect (season_id, service) → anidb_anime_id mapping
    rows = conn.execute(
        "SELECT sei.rowid, sei.season_id, sei.service, sei.name,"
        "  (SELECT eam.anidb_anime_id"
        "   FROM episode e"
        "   JOIN episode_anidb_mapping eam ON eam.episode_id = e.id"
        "   WHERE e.season_id = sei.season_id"
        "   GROUP BY eam.anidb_anime_id"
        "   ORDER BY count(*) DESC LIMIT 1"
        "  ) AS anidb_anime_id"
        " FROM season_external_id sei"
        " WHERE sei.name IS NULL",
    ).fetchall()

    for row in rows:
        anidb_id = row["anidb_anime_id"]
        name = None

        if anidb_id is not None:
            # Look up English official title, fall back to romaji
            title_row = conn.execute(
                "SELECT title FROM anidb_title"
                " WHERE anidb_id = ? AND lang = 'en' AND title_type = 1"
                " LIMIT 1",
                (anidb_id,),
            ).fetchone()
            if title_row is None:
                title_row = conn.execute(
                    "SELECT title FROM anidb_title"
                    " WHERE anidb_id = ? AND lang = 'x-jat' AND title_type = 1"
                    " LIMIT 1",
                    (anidb_id,),
                ).fetchone()
            if title_row:
                name = title_row["title"]

        if name is None:
            # Single-season fallback: use show's display title
            season_row = conn.execute(
                "SELECT sh.primary_title AS pref,"
                "  sh.title_romaji, sh.title_english, sh.title_native,"
                "  (SELECT count(*) FROM season s2"
                "   WHERE s2.show_id = sh.id AND s2.season_number > 0"
                "  ) AS season_count"
                " FROM season s"
                " JOIN show sh ON sh.id = s.show_id"
                " WHERE s.id = ?",
                (row["season_id"],),
            ).fetchone()
            if season_row and season_row["season_count"] == 1:
                pref = season_row["pref"]  # 'romaji', 'english', or 'native'
                name = season_row[f"title_{pref}"] or season_row["title_romaji"]

        if name:
            conn.execute(
                "UPDATE season_external_id SET name = ?"
                " WHERE season_id = ? AND service = ?",
                (name, row["season_id"], row["service"]),
            )
            filled += 1

    if filled:
        conn.commit()
        log.info("backfill_season_names: filled %d names", filled)
    return filled


# ---------------------------------------------------------------------------
# Step 5 — Fill abs_start/abs_end for all tracking spaces
# ---------------------------------------------------------------------------


def fill_season_ranges_bulk(conn: sqlite3.Connection) -> int:
    """Bulk fill ``abs_start``/``abs_end`` on seasons that lack them.

    Extends the per-show ``fill_season_ranges`` to cover all shows in
    one pass and to handle TV shows (which use episode counts instead
    of Sonarr absolute numbering).

    **Anime**: derives from ``MIN``/``MAX(absolute_number)`` per season
    (show-wide Sonarr-style numbering). Same logic as ``fill_season_ranges``
    but batched.

    **TV**: derives from episode counts per season, accumulated in show
    order. Season 1 with 22 episodes → abs_start=1, abs_end=22.
    Season 2 with 13 episodes → abs_start=23, abs_end=35.

    Strictly NULL-only — never overwrites existing ranges.  Skips
    season 0 (specials).  Idempotent and safe to call every tick.

    Returns the number of seasons updated.
    """
    now = util.now_utc_iso()
    updated = 0

    # ── Anime: from absolute_number ──
    anime_seasons = conn.execute(
        "SELECT s.id, s.show_id, s.season_number"
        " FROM season s"
        " JOIN show sh ON sh.id = s.show_id"
        " WHERE sh.tracking_space = 'anime'"
        "   AND s.season_number > 0"
        "   AND s.abs_start IS NULL AND s.abs_end IS NULL",
    ).fetchall()

    for s in anime_seasons:
        range_row = conn.execute(
            "SELECT MIN(absolute_number) AS abs_min,"
            "       MAX(absolute_number) AS abs_max"
            " FROM episode"
            " WHERE show_id = ? AND season = ?"
            "   AND absolute_number IS NOT NULL"
            "   AND absolute_number = CAST(absolute_number AS INTEGER)",
            (s["show_id"], s["season_number"]),
        ).fetchone()
        if range_row["abs_min"] is None:
            continue
        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ?"
            " WHERE id = ?",
            (int(range_row["abs_min"]), int(range_row["abs_max"]), now, s["id"]),
        )
        updated += 1

    # ── TV: from episode counts, accumulated per show ──
    # Group by show, order by season_number, accumulate ranges.
    tv_shows = conn.execute(
        "SELECT DISTINCT s.show_id"
        " FROM season s"
        " JOIN show sh ON sh.id = s.show_id"
        " WHERE sh.tracking_space = 'tv'"
        "   AND s.season_number > 0"
        "   AND s.abs_start IS NULL AND s.abs_end IS NULL",
    ).fetchall()

    for show in tv_shows:
        show_id = show["show_id"]
        # Get ALL seasons for this show (including ones with ranges
        # already set) to compute correct cumulative offsets.
        all_seasons = conn.execute(
            "SELECT s.id, s.season_number, s.abs_start, s.abs_end,"
            "  (SELECT count(*) FROM episode e"
            "   WHERE e.show_id = ? AND e.season = s.season_number"
            "  ) AS ep_count"
            " FROM season s"
            " WHERE s.show_id = ? AND s.season_number > 0"
            " ORDER BY s.season_number",
            (show_id, show_id),
        ).fetchall()

        running = 1
        for s in all_seasons:
            ep_count = s["ep_count"]
            if ep_count == 0:
                continue
            if s["abs_start"] is not None:
                # Already has a range — advance running counter past it
                running = s["abs_end"] + 1
                continue
            abs_start = running
            abs_end = running + ep_count - 1
            conn.execute(
                "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ?"
                " WHERE id = ?",
                (abs_start, abs_end, now, s["id"]),
            )
            running = abs_end + 1
            updated += 1

    if updated:
        conn.commit()
        log.info("fill_season_ranges_bulk: updated %d seasons", updated)
    return updated
