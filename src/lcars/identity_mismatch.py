"""Passive detection for a show/season linked to the wrong real-world
identity — 2026-09-21, found live: "Tantei wa mou, Shindeiru. Season 2"
was actually entirely Detective Opera Milky Holmes content, because its
Sonarr/TVDB link was wrong from the start. Nothing existing catches a
*confident* wrong match — `_ensure_anilist_link` (metadata.py) only
flags when Fribb finds *no* AniList match at all, which is a different,
already-handled failure mode.

One signal, never auto-applied — same safe shape this codebase already
commits to for every other genuine ambiguity (`score_sync.py`'s drift
checks, `show_merge.find_candidate_pairs`): open a `pending_review`, let
a human decide, repair via the already-existing `amendShowArrLink`
(shows.amend_show_arr_link).

AniList ID mismatch — validated against the real Tantei case before this
was written: Fribb's own independent tvdb->anilist resolution for that
season disagreed completely with what was actually stored. Cheap:
Fribb's dataset is local/cached, no network call.

**A second signal (title fuzzy-match: LCARS's own title vs Sonarr's own
series title) was designed, calibrated, and rejected same-night, 2026-
09-21** — not a threshold-tuning problem, a discriminative-power one.
Calibrated against every currently-tracked, Sonarr-linked show's real
title pair: the worst *known-correct* match (a legitimate romaji->
English translation) scored 0.159 (`SequenceMatcher` ratio on
normalized titles). The real Tantei/Milky-Holmes bug scored 0.300 —
*higher* than that genuinely correct match. No threshold separates them;
raw title-string similarity isn't discriminative across a
romaji/translated-English title gap. Logged in NEXT_UP.md as a real,
still-open idea (e.g. episode-count/air-date-pattern consistency instead
of title strings) rather than shipped not-working.

Idempotent the same way score_sync.py already is: `open_or_extend`
accumulates a chain on repeated disagreement, `already_resolved_with`
suppresses re-opening the exact value a human already resolved, and a
genuinely different disagreement (the mismatch itself changed) always
surfaces as new — confirmed this is the wanted behavior with the user
directly, not assumed.

**Real false-positive flood found same night, NOT currently scheduled
automatically** (see ops/scheduler.py's own run_identity_mismatch_once
docstring for the full note): a real run against production flagged 34
seasons, the large majority false positives. Root cause: LCARS's own
sequential `season.season_number` is not the same numbering scheme as
Fribb's raw `season.tvdb` tag once a franchise is split into cours/parts
— e.g. SPY×FAMILY, where Fribb tags both "Part I" and "Part II" as
`season.tvdb: 1` (distinguished only by `episode_offset`), while LCARS
tracks them as `season_number` 1 and 2. Passing LCARS's season_number
straight into `fribb.resolve_season_candidate` assumes the two
numbering schemes are interchangeable, which for a split-cour franchise
they aren't — the same unsolved "collapse multi-show franchises via
abs-episode-range reconciliation" gap the season-subdivision work
(D1-D3, NEXT_UP.md) already describes as not yet built. Callable
manually (`pollIdentityMismatch`) with that caveat in mind; do not
re-wire into the automatic loop until that reconciliation exists."""

import sqlite3

from lcars import fribb, pending_review


def check_anilist_id_mismatch(conn: sqlite3.Connection) -> dict:
    """Signal 1 — for every tracked season with its own `anilist_id` and
    a show-level `tvdb` id, compare against Fribb's own independent
    tvdb->anilist resolution for that exact season number. Disagreement
    opens a `pending_review` (season, field='anilist_id', so it gets the
    same season-AniList-ID inline editor reviews.html already has for
    the unrelated "no match found" case — distinguished by `source`).

    Deliberately does NOT skip `manual_override = 1` seasons: the real
    Tantei case had that flag set on the wrong value already — trusting
    it would have missed exactly the bug this exists to catch."""
    dataset = fribb.load_dataset()
    index = fribb.build_tvdb_index(dataset)

    rows = conn.execute(
        """
        SELECT s.id AS season_id, s.season_number, s.anilist_id,
               sh.id AS show_id, sh.title_romaji,
               sei_tvdb.external_id AS tvdb_id
        FROM season s
        JOIN show sh ON sh.id = s.show_id
        JOIN show_external_id sei_tvdb
          ON sei_tvdb.show_id = sh.id AND sei_tvdb.service = 'tvdb'
        WHERE sh.tracked = 1 AND s.anilist_id IS NOT NULL
        """
    ).fetchall()

    checked = 0
    flagged = 0
    for row in rows:
        try:
            tvdb_id = int(row["tvdb_id"])
        except (TypeError, ValueError):
            continue
        candidate = fribb.resolve_season_candidate(index, tvdb_id, row["season_number"])
        if candidate is None:
            continue  # Fribb has no opinion for this season — not this signal's job
        fribb_anilist_id, _fribb_mal_id = fribb.extract_ids(candidate)
        if fribb_anilist_id is None:
            continue
        checked += 1
        if fribb_anilist_id == row["anilist_id"]:
            continue

        proposed = str(fribb_anilist_id)
        if pending_review.already_resolved_with(
            conn, "season", row["season_id"], "anilist_id", proposed
        ):
            continue
        pending_review.open_or_extend(
            conn,
            "season",
            row["season_id"],
            "anilist_id",
            "fribb_identity_mismatch",
            str(row["anilist_id"]),
            proposed,
        )
        flagged += 1

    conn.commit()
    return {"checked": checked, "flagged": flagged}
