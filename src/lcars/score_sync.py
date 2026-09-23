"""AniList score reverse-sync (2026-08-27) — the inbound half of
LCARS's score round-trip: LCARS → AniList/MAL is already handled by
`_push_season_score`/`_push_mal_season_score` in resolvers.py; this
module adds the other direction.

Design (chosen with user, 2026-08-27):
  LCARS is source of truth.  A score change made directly on AniList
  is detected here, recorded as a `pending_review` (field "score",
  entity_type "season"), and surfaced in Data's review screen.  The
  human confirms → Data calls `setSeasonScore` / `setScore` with
  `proposedValueChain[-1]`, then `resolvePendingReview`.  No score is
  ever auto-applied without human review.

Scale guard:
  LCARS 0–20 quarter-point × 5 = AniList POINT_100.  AniList
  typically stores POINT_100 as integer.  We compare
  `int(anilist_score) == round(effective_lcars * 5)` so that a
  quarter-point LCARS push (e.g. 17.25 × 5 = 86.25, stored as 86)
  doesn't produce a false positive on every tick.  If AniList
  *does* preserve the float, `int()` strips the fractional part and
  the same comparison still holds for any value LCARS can push (all
  multiples of 0.25 × 5 are representable and round cleanly).

  A `0` AniList score means "unscored" per AniList convention; we
  skip those entries entirely — they are not an external change.

  MAL (0–10 integer): `check_mal_score_drift` below.  MAL has no
  activity-feed equivalent so the full list is fetched every tick.
  Scale: MAL 0–10 integer, LCARS 0–20.  LCARS pushes
  `round(effective_lcars / 2)` (banker's rounding).  Guard: if
  `mal_score == round(effective_lcars / 2)` the round-trip is clean —
  no false positive even when 17.0 → 8 (bankers rounds 8.5 → 8) then
  8 reads back.  A `0` MAL score means "unscored"; we skip those.

`already_resolved_with` guard:
  The mismatch value stored in `proposed_value_chain` is the
  *inbound AniList score converted to LCARS scale* (a string like
  "17.0").  If the human resolved a review for exactly that proposed
  value and AniList still shows the same score on the next tick, we
  don't re-open — same idempotency pattern as S5's width check.  A
  genuinely *different* external score (a new mismatch value) is not
  suppressed.
"""

import sqlite3

from lcars import anilist_client, config, mal_client, pending_review


def check_anilist_score_drift(conn: sqlite3.Connection) -> dict[str, int]:
    """Detect AniList score drift vs LCARS's current effective score.

    Fetches the viewer's full AniList list (one batched call), then for
    each AniList-linked LCARS season checks whether the AniList score is
    consistent with what LCARS would have pushed.  Opens a
    `pending_review` (field "score") when they disagree.

    Returns {"checked": N, "flagged": M} where `checked` is the number
    of seasons that had a non-zero AniList score and were actually
    compared, and `flagged` is the number where a pending_review was
    opened or extended this call.

    No-ops (returns zeros) when no AniList token is configured."""
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return {"checked": 0, "flagged": 0}

    # One batched call — same dataset the reconcile sweep uses.
    entries = anilist_client.fetch_my_anime_list(cfg.anilist_access_token)

    # Build lookup: anilist_id → integer score (skip unscored 0s).
    anilist_scores: dict[int, int] = {
        e["anilist_id"]: int(e["score"])
        for e in entries
        if e.get("score")  # falsy catches both 0 and None; see docstring on fetch_my_anime_list
    }

    if not anilist_scores:
        return {"checked": 0, "flagged": 0}

    # Fetch every LCARS season that has an AniList link.
    rows = conn.execute(
        "SELECT s.id, s.score AS season_score, sh.score AS show_score,"
        "       sex.external_id AS anilist_id"
        " FROM season s"
        " JOIN show sh ON sh.id = s.show_id"
        " JOIN season_external_id sex"
        "   ON sex.season_id = s.id AND sex.service = 'anilist'",
    ).fetchall()

    checked = 0
    flagged = 0

    for r in rows:
        anilist_id = int(r["anilist_id"])
        anilist_score = anilist_scores.get(anilist_id)
        if anilist_score is None:
            continue  # unscored externally or not on the list

        # 2026-09-20 fix: skip only when a season has no score of its own
        # AND a show-level score exists to falsely stand in for it — that
        # combination was the real source of the ~238 noise reviews (any
        # multi-season show scored once at the show level but rated
        # differently per season on AniList flagged every season as
        # "drift," even though LCARS never actually asserted a score for
        # that specific season). When NEITHER exists, LCARS genuinely has
        # no opinion on this season yet, and a remote score there is real,
        # worth-flagging new information — that case still gets compared.
        if r["season_score"] is None and r["show_score"] is not None:
            continue

        checked += 1
        effective_lcars = (
            r["season_score"] if r["season_score"] is not None else r["show_score"]
        )

        # What would LCARS have pushed to AniList? None (neither season
        # nor show is scored) means LCARS has no assertion at all — any
        # real AniList score there is new inbound information.
        # round() uses banker's rounding, matching Python float behaviour.
        expected_anilist = (
            round(effective_lcars * 5) if effective_lcars is not None else None
        )

        # int() strips AniList float residue (e.g. 86.25 → 86) so a
        # quarter-point push that AniList snapped doesn't false-positive.
        if expected_anilist is not None and int(anilist_score) == expected_anilist:
            continue  # consistent — LCARS pushed this, round-trip is clean

        # Discrepancy: either LCARS has no score and AniList does, or the
        # AniList score no longer matches what LCARS pushed.
        proposed_lcars = str(round(anilist_score / 5, 2))

        if pending_review.already_resolved_with(
            conn, "season", r["id"], "score", proposed_lcars
        ):
            continue  # human already reviewed and resolved this exact value

        previous_str = str(effective_lcars) if effective_lcars is not None else None
        pending_review.open_or_extend(
            conn,
            "season",
            r["id"],
            "score",
            "anilist_score_drift",
            previous_str,
            proposed_lcars,
        )
        flagged += 1

    conn.commit()
    return {"checked": checked, "flagged": flagged}


def check_mal_score_drift(conn: sqlite3.Connection) -> dict[str, int]:
    """Detect MAL score drift vs LCARS's current effective score.

    MAL has no activity-feed equivalent, so the full list is fetched on
    every call (same cadence as `check_anilist_score_drift`).  For each
    MAL-linked LCARS season, checks whether the MAL score is consistent
    with what LCARS would have pushed (`round(effective_lcars / 2)`, the
    same banker's-rounding formula `_push_mal_season_score` uses).
    Opens a `pending_review` (field "score") when they disagree.

    Returns {"checked": N, "flagged": M} — same shape as
    `check_anilist_score_drift`.

    No-ops (returns zeros) when no MAL token is configured."""
    cfg = config.get_current()
    if not cfg.mal_access_token:
        return {"checked": 0, "flagged": 0}

    # One batched call — pages internally, returns the whole list flat.
    entries = mal_client.fetch_my_list(cfg.mal_access_token)

    # Build lookup: mal_id → integer score.  MAL score 0 means "unscored"
    # by MAL's own convention; skip those (same reasoning as AniList's 0).
    mal_scores: dict[int, int] = {
        e["mal_id"]: int(e["score"])
        for e in entries
        if e.get("score")  # falsy catches 0 and None
    }

    if not mal_scores:
        return {"checked": 0, "flagged": 0}

    # Every LCARS season with a MAL link (mal_id is a direct column on season).
    rows = conn.execute(
        "SELECT s.id, s.score AS season_score, sh.score AS show_score, s.mal_id"
        " FROM season s"
        " JOIN show sh ON sh.id = s.show_id"
        " WHERE s.mal_id IS NOT NULL",
    ).fetchall()

    checked = 0
    flagged = 0

    for r in rows:
        mal_id = int(r["mal_id"])
        mal_score = mal_scores.get(mal_id)
        if mal_score is None:
            continue  # not on the MAL list or unscored

        # 2026-09-20 fix: skip only when a season has no score of its own
        # AND a show-level score exists to falsely stand in for it — same
        # reasoning as check_anilist_score_drift's own fix. When NEITHER
        # exists, LCARS genuinely has no opinion on this season, and a
        # real MAL score there is worth flagging, not suppressing.
        if r["season_score"] is None and r["show_score"] is not None:
            continue

        checked += 1
        effective_lcars = (
            r["season_score"] if r["season_score"] is not None else r["show_score"]
        )

        # What would LCARS have pushed to MAL?  round() is banker's rounding,
        # matching `_push_mal_season_score` exactly. None (neither season
        # nor show scored) means no assertion at all.
        expected_mal = round(effective_lcars / 2) if effective_lcars is not None else None

        # Consistent when MAL is within one conversion step of LCARS, not
        # only when it equals round(): MAL's 0-10 integer can't hold half a
        # step, so an LCARS 13.0 (6.5 on MAL's scale) is equally right as 6
        # or 7 — MAL may hold either (mirrored from AniList's 65, set by
        # hand, or rounded the other way). 2026-09-23: exact-match flagged
        # Goblin's Crown and Laid-Back Camp S3 (13.0 vs MAL 7) as drift.
        if expected_mal is not None and abs(mal_score * 2 - effective_lcars) <= 1:
            continue

        # Discrepancy: either LCARS has no score and MAL does, or the MAL
        # score no longer matches what LCARS pushed.
        # Reverse: MAL 0–10 → LCARS 0–20 (integer × 2; always exact).
        proposed_lcars = str(float(mal_score * 2))

        if pending_review.already_resolved_with(
            conn, "season", r["id"], "score", proposed_lcars
        ):
            continue  # human already reviewed and resolved this exact value

        previous_str = str(effective_lcars) if effective_lcars is not None else None
        pending_review.open_or_extend(
            conn,
            "season",
            r["id"],
            "score",
            "mal_score_drift",
            previous_str,
            proposed_lcars,
        )
        flagged += 1

    conn.commit()
    return {"checked": checked, "flagged": flagged}
