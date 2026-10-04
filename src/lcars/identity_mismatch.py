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
codebase uses everywhere else.

**Redesigned 2026-10-04 (user), for the rebuilt data.** The position method above assumed
LCARS's season number counted cours in release order. Since the rebuild, a season's number is its
**TVDB season number** (a split cour is a `part` level inside it), and every TVDB season has a
row — including ones with no AniList entry (Gintama: TVDB seasons 1–10, Fribb lists 5). Position
then no longer lines up: run on the production copy the position method flagged 58 seasons, almost
all false, and a level with no season number (a special) crashed it (`None < 1`) on every hourly
sweep since the cutover. Now: for each *whole numbered TVDB season* with an AniList id, take the
Fribb entries for the show's TVDB id whose own `season.tvdb` equals that number (not specials or
movies); exactly one AniList id -> compare; none (Fribb has no entry for that season) or several
(a split cour) -> **no opinion**. Part levels and levels with no number are not checked. On the
production copy: 1 disagreement in 1,373 seasons. Decision record:
DECISION-identity-check-2026-10-04.md."""

import sqlite3

from lcars import fribb, pending_review


def _fribb_anilist_ids(candidates: list[dict], tvdb_season: int) -> set[int]:
    """The AniList ids Fribb holds for one TVDB season number of a show (specials and movies
    left out, same as `fribb.enumerate_real_seasons`)."""
    found = set()
    for c in candidates:
        if c.get("type") in ("SPECIAL", "MOVIE"):
            continue
        if (c.get("season") or {}).get("tvdb") != tvdb_season:
            continue
        anilist_id, _mal_id = fribb.extract_ids(c)
        if anilist_id is not None:
            found.add(anilist_id)
    return found


def check_anilist_id_mismatch(conn: sqlite3.Connection) -> dict:
    """Signal 1 — for every tracked whole TVDB season (`kind = 'tvdb_season'`, with a season
    number) that has its own `anilist_id`, compare it with Fribb's own independent answer for
    that TVDB season number (`_fribb_anilist_ids`). A split cour (several Fribb entries on one
    TVDB season) or a season Fribb has no entry for gives no opinion. Disagreement opens a
    `pending_review` (season, field='anilist_id', so it gets the same season-AniList-ID inline
    editor reviews.html already has for the unrelated "no match found" case — distinguished
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
          AND s.kind = 'tvdb_season' AND s.season_number IS NOT NULL
        """
    ).fetchall()

    checked = 0
    flagged = 0
    for row in rows:
        try:
            tvdb_id = int(row["tvdb_id"])
        except (TypeError, ValueError):
            continue
        expected = _fribb_anilist_ids(index.get(tvdb_id, []), row["season_number"])
        if len(expected) != 1:
            continue  # none (no Fribb entry for this season) or several (split cour): no opinion
        (fribb_anilist_id,) = expected
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
