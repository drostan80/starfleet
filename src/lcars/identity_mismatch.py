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

**Real false-positive flood found same night, fixed same night, 2026-09-
21** (see git history for the full incident): a first version passed
LCARS's own sequential `season.season_number` straight into
`fribb.resolve_season_candidate(tvdb_id, season_number)`, assuming that
number was the same thing as Fribb's raw `season.tvdb` tag. It isn't,
once a franchise is split into cours/parts — e.g. SPY×FAMILY, where
Fribb tags both "Part I" and "Part II" as `season.tvdb: 1`
(distinguished only by `episode_offset`), while LCARS tracks them as
`season_number` 1 and 2. A real run against production flagged 34
seasons this way, the large majority false positives.

**Fix**: `_resolve_by_position` below (deliberately separate from
`fribb.resolve_season_candidate` — that function's own comment records
that tightening its single-candidate fallback was tried live 2026-07-09,
reverted the same day, 28 regressions; this module needs strict,
unambiguous verification, that one needs lenient best-effort backfill,
and they should not share an implementation). It orders every real
(non-special, non-movie) candidate for a tvdb_id by
`(season.tvdb, episode_offset.tvdb)` and takes the LCARS season_number'th
one positionally — the same "count sequential parts in release order"
logic LCARS's own season_number already uses. Validated against both
real cases before shipping: SPY×FAMILY season 2 now correctly resolves
to "Part II" (no mismatch); the Tantei stub still correctly flags (its
season 1 resolves to Milky Holmes' real season 1, disagreeing with what
was stored). Ambiguous cases (two candidates landing on the same sort
key) return None rather than guess, same "never guess" convention this
codebase uses everywhere else."""

import sqlite3

from lcars import fribb, pending_review


def _resolve_by_position(candidates: list[dict], lcars_season_number: int) -> dict | None:
    """Strict, position-based season resolver — see module docstring for
    why this is separate from fribb.resolve_season_candidate."""
    numbered = [
        c
        for c in candidates
        if c.get("type") not in ("SPECIAL", "MOVIE") and (c.get("season") or {}).get("tvdb", 0) > 0
    ]

    def sort_key(c: dict) -> tuple[int, int]:
        season_tvdb = c["season"]["tvdb"]
        offset = (c.get("episode_offset") or {}).get("tvdb") or 0
        return (season_tvdb, offset)

    numbered.sort(key=sort_key)
    keys = [sort_key(c) for c in numbered]
    if len(keys) != len(set(keys)):
        return None  # two candidates share a sort position — genuinely ambiguous, don't guess
    if lcars_season_number < 1 or lcars_season_number > len(numbered):
        return None
    return numbered[lcars_season_number - 1]


def check_anilist_id_mismatch(conn: sqlite3.Connection) -> dict:
    """Signal 1 — for every tracked season with its own `anilist_id` and
    a show-level `tvdb` id, compare against Fribb's own independent
    tvdb->anilist resolution for that exact position in the show's own
    release order (`_resolve_by_position`, not raw season_number).
    Disagreement opens a `pending_review` (season, field='anilist_id',
    so it gets the same season-AniList-ID inline editor reviews.html
    already has for the unrelated "no match found" case — distinguished
    by `source`).

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
        candidates = index.get(tvdb_id, [])
        candidate = _resolve_by_position(candidates, row["season_number"])
        if candidate is None:
            continue  # Fribb has no unambiguous opinion for this position — not this signal's job
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
