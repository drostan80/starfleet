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

from lcars import anidb, fribb, ids, pending_review, season_ranges, sonarr_match, util


def reconcile_season(
    conn, show_id: str, season_number: int, *, tvdb_coords=None
) -> dict:
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

    **Identity comes from absolute order, not the season number
    (2026-09-23).** LCARS subdivides seasons to the finest source, so its
    season number is not TVDB's (Slime: TVDB S2 became LCARS S2+S3, and
    looking up Fribb by `(tvdb_id, season_number)` put the 2026 season's
    AniList id on LCARS S4, the 2024 season). The candidate is now
    derived by `_derive_ids` below, most authoritative first: Memory
    Alpha's own AniDB mapping for this season's episodes, then the
    episodes' real TVDB coordinates through Anime-Lists, and only for a
    show with no captured coordinates and no sign of divergence the old
    `(tvdb_id, season_number)` Fribb lookup. Evidence that spans more
    than one AniDB entry makes no claim at all — the stored value is
    left untouched rather than cleared.

    `tvdb_coords` — the real Sonarr/TVDB `(season, episode)` pairs the
    caller just routed onto this LCARS season (`metadata._ensure_seasons`
    on a Sonarr fetch, before the episode rows exist).

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
        status, list_sync = season_ranges.auto_season_fields(conn, show_id, season_number)
        conn.execute(
            "INSERT INTO season"
            " (id, show_id, season_number, status, anilist_id, mal_id, source, matched,"
            "  manual_override, last_reconciled_at, created_at, updated_at, list_sync)"
            " VALUES (?, ?, ?, ?, NULL, NULL, 'unmatched', 0, 0, ?, ?, ?, ?)",
            (season_id, show_id, season_number, status, now, now, now, list_sync),
        )
        conn.commit()
        return get_season(conn, season_id)

    tvdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    derived = _derive_ids(
        conn,
        show_id,
        season_number,
        int(tvdb_row["external_id"]) if tvdb_row is not None else None,
        tvdb_coords,
    )
    if derived is None and existing is not None and (
        existing["anilist_id"] is not None or existing["mal_id"] is not None
    ):
        # 2026-09-23 — "no candidate" is absence of evidence, not evidence
        # that the stored link is wrong: never clear a stored link on it.
        # A wrong stored link is identity_mismatch's job to flag.
        derived = _NO_CLAIM
    if derived is _NO_CLAIM:
        if existing is not None:
            conn.execute(
                "UPDATE season SET last_reconciled_at = ?, updated_at = ? WHERE id = ?",
                (now, now, existing["id"]),
            )
            conn.commit()
            return get_season(conn, existing["id"])
        derived = None
    anilist_id, mal_id = derived if derived is not None else (None, None)
    matched = derived is not None
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
        status, list_sync = season_ranges.auto_season_fields(
            conn, show_id, season_number, anilist_id
        )
        conn.execute(
            "INSERT INTO season"
            " (id, show_id, season_number, status, anilist_id, mal_id, source, matched,"
            "  manual_override, last_reconciled_at, created_at, updated_at, list_sync)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (season_id, show_id, season_number, status,
             anilist_id, mal_id, source, matched, now, now, now, list_sync),
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


_NO_CLAIM = object()


def _derive_ids(conn, show_id: str, season_number: int, tvdb_id, tvdb_coords):
    """(anilist_id, mal_id) for this LCARS season, None for a genuine "no
    candidate", or `_NO_CLAIM` when the evidence is ambiguous. See
    `reconcile_season`'s docstring for the order and why."""
    if season_number is None or season_number <= 0:
        return None

    # 1. Memory Alpha: the AniDB entries this season's episodes map to.
    anidb_ids = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT m.anidb_anime_id FROM episode e"
            " JOIN episode_anidb_mapping m ON m.episode_id = e.id"
            " WHERE e.show_id = ? AND e.season = ? AND e.kind = 'regular'"
            "   AND m.anidb_season = 1",
            (show_id, season_number),
        ).fetchall()
    }

    # 2. The episodes' real TVDB coordinates -> Anime-Lists -> AniDB.
    coords = set(tvdb_coords or ())
    if tvdb_id is not None and not coords:
        coords = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT sonarr_season, sonarr_episode FROM episode"
                " WHERE show_id = ? AND season = ? AND kind = 'regular'"
                "   AND sonarr_season IS NOT NULL AND sonarr_season > 0",
                (show_id, season_number),
            ).fetchall()
        }
    if not anidb_ids and tvdb_id is not None and coords:
        anidb_ids = anidb.anidb_ids_for_tvdb_coords(conn, tvdb_id, coords)

    if len(anidb_ids) > 1:
        return _NO_CLAIM  # spans several AniDB entries — never guess which one
    dataset = None
    if len(anidb_ids) == 1:
        dataset = fribb.load_dataset()
        ids_ = fribb.resolve_ids_for_anidb(fribb.build_anidb_index(dataset), anidb_ids.pop())
        if ids_["anilist_id"] is not None or ids_["mal_id"] is not None:
            return ids_["anilist_id"], ids_["mal_id"]

    if tvdb_id is None:
        return None
    if not coords and not _season_has_episodes(conn, show_id, season_number):
        # 2026-09-23 — a season with no episodes yet was created from
        # Fribb's own season *order* (`season_ranges.ensure_fribb_season_
        # rows` / `fribb.enumerate_real_seasons`, the 2026-09-21 season-
        # number-gap fix), so that order is the only evidence there is.
        # Looking it up by TVDB season number instead — the model those
        # rows were never created with — cleared 47 such seasons in one
        # weekly pass (JoJo, Pokémon, NieR...), and would have kept
        # ping-ponging with the creator forever. No positional answer:
        # no claim, the stored value stays.
        dataset = dataset or fribb.load_dataset()
        numbered = fribb.enumerate_real_seasons(
            fribb.build_tvdb_index(dataset).get(tvdb_id, [])
        )
        if numbered is None or not 1 <= season_number <= len(numbered):
            return _NO_CLAIM
        return fribb.extract_ids(numbered[season_number - 1])
    if coords:
        tvdb_seasons = {c[0] for c in coords}
        if len(tvdb_seasons) != 1:
            return _NO_CLAIM
        tvdb_season = tvdb_seasons.pop()
    elif sonarr_match.show_numbering_diverged(conn, show_id):
        return _NO_CLAIM  # no coordinates, and LCARS's number is known not to be TVDB's
    else:
        tvdb_season = season_number  # nothing says they differ — ordinary show
    dataset = dataset or fribb.load_dataset()
    # Strict: the candidate's own TVDB season must match. Not
    # `fribb.resolve_season_candidate`, whose "only one Fribb entry for
    # this series -> return it for any season" shortcut would stamp that
    # one entry onto every season of the show (found 2026-09-23 on a
    # year-numbered show, seasons 2021/2022/2024, and on a show still
    # carrying another series' season rows).
    matches = [
        c
        for c in fribb.build_tvdb_index(dataset).get(tvdb_id, [])
        if (c.get("season") or {}).get("tvdb") == tvdb_season
    ]
    if len(matches) != 1:
        return None
    return fribb.extract_ids(matches[0])


def _season_has_episodes(conn, show_id: str, season_number: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? AND season = ? LIMIT 1",
        (show_id, season_number),
    ).fetchone()
    return row is not None


def get_season(conn, season_id: str) -> dict | None:
    """The single definition — `resolvers.py` imports this rather than
    keeping its own copy. Deduplicated 2026-08-09: the two had drifted to
    different signatures (`dict` here, `dict | None` there), which is the
    kind of divergence that eventually bites at the one call site that
    assumed the other one's contract."""
    row = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    return dict(row) if row else None
