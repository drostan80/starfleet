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
"""

import sqlite3

from lcars import util


def upsert_season_external_id(
    conn: sqlite3.Connection,
    season_id: str,
    anilist_id: int | None,
    mal_id: int | None,
    now: str,
) -> None:
    """Mirror anilist_id/mal_id into season_external_id.

    Uses INSERT ... ON CONFLICT so it's idempotent — safe to call on
    every write, not just first-time. Deletes a mapping row if the
    caller sets the id to NULL (the season was unlinked).
    """
    for service, ext_id in [("anilist", anilist_id), ("mal", mal_id)]:
        if ext_id is not None:
            conn.execute(
                "INSERT INTO season_external_id"
                " (season_id, service, external_id, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (season_id, service)"
                " DO UPDATE SET external_id = excluded.external_id",
                (season_id, service, ext_id, now),
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
