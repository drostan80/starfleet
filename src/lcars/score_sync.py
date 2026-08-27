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

  MAL (0–10 integer) is not yet handled here: MAL has no activity-feed
  equivalent and the round-trip is lossier (÷2 with banker's rounding).
  A future slice can add `check_mal_score_drift` in this module.

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

from lcars import anilist_client, config, pending_review


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

        checked += 1

        # Effective LCARS score: season's own, else show's.
        effective_lcars = (
            r["season_score"] if r["season_score"] is not None else r["show_score"]
        )

        # What would LCARS have pushed to AniList?
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

        previous_str = (
            str(effective_lcars) if effective_lcars is not None else None
        )
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
