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

import sqlite3

from lcars import anilist_client, pending_review, util

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
