"""Season id-mapper reconciliation core — SCOPE.md §5.5, BUILD_PLAN.md A.4/A.20.

Extracted out of `resolvers.py` during A.20 (2026-08-09 consolidation
pass), same reasoning `pending_review.py` was already extracted for
during A.8: `metadata.py` needs this exact logic too (to reconcile a
newly-Sonarr-discovered season immediately, not just on an explicit
`reconcileSeasonMapping` GraphQL call), and a resolvers.py<->metadata.py
circular import isn't worth introducing to share one function. The
`reconcileSeasonMapping` mutation itself is now a thin wrapper around
`reconcile_season()` below — no behavior change for that call path,
confirmed by the pre-existing `test_server.py`/`test_fribb.py` coverage
still passing unchanged.
"""

from lcars import fribb, ids, pending_review, season_ranges, util


def reconcile_season(conn, show_id: str, season_number: int) -> dict:
    """Attempts automatic derivation against the Fribb/anime-lists
    dataset (§5.5, `fribb.py`) for this season and applies the result
    immediately (§3 principle 1) — except when the row is already
    manual_override, which stays fully protected (§3 principle 6) and
    doesn't even get a PendingReview logged for the disagreement
    (asked/confirmed 2026-08-08, A.4: a review entry for something
    already manually decided doesn't serve pending_review's "later
    human awareness" purpose — last_reconciled_at still updates so the
    row shows as checked). A genuine value change on a non-override
    row — including a first-time "no candidate found", per §5.5's own
    "stays usable locally in an unmapped state" framing — opens/
    extends a PendingReview; an unchanged re-check (same value, or
    still no candidate) does not, so repeated calls (whether the
    explicit mutation or A.20's own on-demand call from a Sonarr fetch)
    don't spam the review queue. No require_client() — no changed_by-
    style column exists on `season` to record it, and `source` already
    says the data came from 'fribb', not who triggered the call.

    Caller's responsibility: `show_id` must already be a real show
    (both call sites — the mutation and A.20's Sonarr-fetch path —
    already hold a real `show` row before calling this).

    `tracking_space != 'anime'` (a plain tv show) is skipped the same
    way season 0 already was (A.20, `_ensure_seasons`' own comment):
    the Fribb dataset only maps anime, so a tv show's season can never
    produce a candidate — every single one was silently opening a
    permanently-unresolvable `anilist_id` pending_review the first time
    Sonarr reported it. Found 2026-08-12: 318 of 344 open season-level
    `anilist_id` reviews belonged to `tracking_space = 'tv'` shows
    (confirmed via a direct query, not a sample) — this is the fix.
    """
    now = util.now_utc_iso()

    show_row = conn.execute("SELECT tracking_space FROM show WHERE id = ?", (show_id,)).fetchone()
    is_anime = show_row is not None and show_row["tracking_space"] == "anime"

    existing_row = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    existing = dict(existing_row) if existing_row else None

    if existing is not None and existing["manual_override"]:
        conn.execute(
            "UPDATE season SET last_reconciled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, existing["id"]),
        )
        conn.commit()
        return get_season(conn, existing["id"])

    if not is_anime:
        if existing is not None:
            conn.execute(
                "UPDATE season SET last_reconciled_at = ?, updated_at = ? WHERE id = ?",
                (now, now, existing["id"]),
            )
            conn.commit()
            return get_season(conn, existing["id"])
        season_id = ids.generate_id(conn, "z")
        status = season_ranges.inherit_season_status(conn, show_id)
        conn.execute(
            "INSERT INTO season"
            " (id, show_id, season_number, status, anilist_id, mal_id, source, matched,"
            "  manual_override, last_reconciled_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, NULL, NULL, 'unmatched', 0, 0, ?, ?, ?)",
            (season_id, show_id, season_number, status, now, now, now),
        )
        conn.commit()
        return get_season(conn, season_id)

    tvdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    candidate = None
    if tvdb_row is not None:
        dataset = fribb.load_dataset()
        index = fribb.build_tvdb_index(dataset)
        tvdb_id = int(tvdb_row["external_id"])
        candidate = fribb.resolve_season_candidate(index, tvdb_id, season_number)
    anilist_id, mal_id = fribb.extract_ids(candidate)
    matched = candidate is not None
    source = "fribb" if matched else "unmatched"

    if existing is not None:
        season_id = existing["id"]
        if existing["anilist_id"] != anilist_id:
            pending_review.open_or_extend(
                conn, "season", season_id, "anilist_id", "fribb", existing["anilist_id"], anilist_id
            )
        if existing["mal_id"] != mal_id:
            pending_review.open_or_extend(
                conn, "season", season_id, "mal_id", "fribb", existing["mal_id"], mal_id
            )
        conn.execute(
            "UPDATE season"
            " SET anilist_id = ?, mal_id = ?, source = ?, matched = ?,"
            "     last_reconciled_at = ?, updated_at = ?"
            " WHERE id = ?",
            (anilist_id, mal_id, source, matched, now, now, season_id),
        )
    else:
        season_id = ids.generate_id(conn, "z")
        status = season_ranges.inherit_season_status(conn, show_id)
        conn.execute(
            "INSERT INTO season"
            " (id, show_id, season_number, status, anilist_id, mal_id, source, matched,"
            "  manual_override, last_reconciled_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (season_id, show_id, season_number, status, anilist_id, mal_id, source, matched, now, now, now),
        )
        if not matched:
            pending_review.open_or_extend(
                conn, "season", season_id, "anilist_id", "fribb", None, None
            )

    # S2 dual-write: mirror into season_external_id so the mapping table
    # stays current going forward (S3 reads from it; see season_ranges.py).
    season_ranges.upsert_season_external_id(conn, season_id, anilist_id, mal_id, now)

    conn.commit()
    return get_season(conn, season_id)


def get_season(conn, season_id: str) -> dict | None:
    """The single definition — `resolvers.py` imports this rather than
    keeping its own copy. Deduplicated 2026-08-09: the two had drifted to
    different signatures (`dict` here, `dict | None` there), which is the
    kind of divergence that eventually bites at the one call site that
    assumed the other one's contract."""
    row = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    return dict(row) if row else None
