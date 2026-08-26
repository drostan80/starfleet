"""One-time historical import — AniList personal scores -> LCARS,
2026-08-26 (NEXT_UP.md's PC.2, AniList-score half; the Trakt-history
half shipped 2026-08-18 as `import_trakt_history.py`, the MAL-legacy-
scores-unique-to-MAL half is still open on that same line).

**What PC.2 says for scores**: "score should be taken first and
primarily from AniList." This fills LCARS scores that are currently
*missing* from what AniList already knows — it never overwrites a score
LCARS already has (a manual `setScore`/`setSeasonScore`, or an earlier
run of this script). AniList is the source; LCARS is the sink. This is
the only direction data flows *in* — nothing here pushes back.

**Why a script, not a mutation** (identical reasoning to the Trakt
import): PC.2 is a one-time historical import for one person's account.
Routing it through `setScore`/`setSeasonScore` would be actively wrong
here, not just heavyweight — those resolvers call `_push_show_score`/
`_push_mal_show_score`, which would push every imported score straight
back to the user's live AniList *and* MAL accounts (hundreds of
mutations, the MAL ones not even round-trips). The data is *coming from*
AniList; pushing it back is a no-op at best and a scale-mismatch
corruption at worst. This writes `season.score`/`show.score`/
`score_change` rows by direct SQL and makes no outbound write of any
kind.

**Score scale** (checked, not assumed): LCARS score is 0-20 in
quarter-point steps (DATABASE_GUIDE.md), AniList is `POINT_100`. The
existing push (`resolvers._push_season_score`) sends `score * 5`, so the
inverse is `anilist_score / 5`. That `POINT_100` assumption has only
ever been *implied* by the push working in production, so this asserts
it explicitly (`fetch_score_format` == `POINT_100`) before writing a
single row — a `POINT_10` account would silently hand back 8.7 where
this expects 87. `anilist_score / 5` lands on 0.2 steps (87 -> 17.4),
off LCARS's quarter-point grid, so every value is re-rounded with
`setScore`'s own `round(x * 4) / 4` rule. That makes the round-trip
lossy in principle (87 -> 17.5 -> 87.5 if some later unrelated
`setScore` ever re-pushes it), the same documented-asymmetry tradeoff
the codebase already accepts elsewhere; an import that preserved raw
POINT_100 would instead corrupt the grid every other part of the system
assumes.

**AniList `0` is "unscored", not a real zero** (checked live before
trusting it): AniList returns `score: 0` for an entry the user simply
never scored, not null. LCARS's own `setScore` treats `0.0` as a
legitimate score, so importing naively would stamp a real `0.0` onto
every unscored entry. Every `score in (None, 0)` is treated as absent
and skipped.

**Two passes, season then show** (matching where AniList actually holds
the data):
  - *Season pass*: `anilist_id` lives on `season`, one AniList entry per
    linked season — this is the canonical granularity, and exactly what
    `_push_season_score` reads. Fills `season.score` for every linked
    season whose score is currently NULL and whose AniList entry has a
    real score. No `score_change` row: that table is show-level only
    (`show_id`, no season column), so per-season history has nowhere to
    live — the same reason `setSeasonScore` writes none either.
  - *Show pass*: `show.score` is what the Data client actually displays
    as the headline "Score" (`app._lcars_score_text` reads `show.score`
    with no season fallback), so it's filled too, for anime shows whose
    score is NULL. A show can link several seasons with *different*
    AniList scores; deriving one show-level number from divergent season
    scores would be a guess, so this only sets `show.score` when the
    linked seasons' (rounded) scores are unambiguous — a single scored
    season, or several that agree. Divergent shows are reported and
    their `show.score` left untouched for the user to set by hand; their
    per-season scores were still filled by the season pass, so no real
    data is lost. Each show-level write records one `score_change` row
    (`changed_by='anilist_import'`, previous_score NULL since we only
    touch unscored shows) — symmetry with the Trakt import's own
    `status_change` rows at `changed_by='trakt_import'`.

**Idempotent by construction**: only ever touches rows whose score is
currently NULL, so a second run is a near-total no-op (it re-reads
AniList and finds everything already filled). Never overwrites, never
double-writes.

**Dry run is a real run against a throwaway copy** (identical to the
Trakt import): `--dry-run` copies the DB to a temp file and runs the
real `--apply` code path against the copy, then discards it — the
report is byte-for-byte what a real run does. It still calls the live
AniList API (that read has no side effects), so it needs the real token
too.

Usage:
    python scripts/import_anilist_scores.py --db PATH --dry-run
    python scripts/import_anilist_scores.py --db PATH --apply
    (token comes from the same lcars.ini the server reads; --token overrides)
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lcars import anilist_client, config, ids, util

_EXPECTED_SCORE_FORMAT = "POINT_100"


def _to_lcars_score(anilist_point_100: float) -> float:
    """AniList POINT_100 -> LCARS 0-20, snapped to the quarter-point grid
    exactly as `resolvers.resolve_set_score` does."""
    raw = anilist_point_100 / 5
    return round(raw * 4) / 4


def _anilist_scores_by_id(token: str) -> dict[int, float]:
    """{anilist_id -> POINT_100 score} for every entry with a *real*
    score (0/None dropped as unscored — see module docstring)."""
    scores: dict[int, float] = {}
    for entry in anilist_client.fetch_my_anime_list(token):
        raw = entry.get("score")
        if raw:  # None or 0 -> unscored, skip
            scores[entry["anilist_id"]] = raw
    return scores


def run_season_pass(conn: sqlite3.Connection, anilist_scores: dict[int, float]) -> dict:
    filled, skipped_already_scored, skipped_no_anilist_score = 0, 0, 0
    now = util.now_utc_iso()
    rows = conn.execute(
        "SELECT id, anilist_id, score FROM season WHERE anilist_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        if row["score"] is not None:
            skipped_already_scored += 1
            continue
        raw = anilist_scores.get(row["anilist_id"])
        if raw is None:
            skipped_no_anilist_score += 1
            continue
        conn.execute(
            "UPDATE season SET score = ?, updated_at = ? WHERE id = ?",
            (_to_lcars_score(raw), now, row["id"]),
        )
        filled += 1
    conn.commit()
    return {
        "seasons_linked": len(rows),
        "filled": filled,
        "skipped_already_scored": skipped_already_scored,
        "skipped_no_anilist_score": skipped_no_anilist_score,
    }


def run_show_pass(conn: sqlite3.Connection, anilist_scores: dict[int, float]) -> dict:
    filled, skipped_already_scored, skipped_no_data = 0, 0, 0
    divergent: list[tuple[str, str, list[float]]] = []
    now = util.now_utc_iso()
    shows = conn.execute(
        "SELECT id, score,"
        " COALESCE(title_english, title_romaji, title_native, id) AS display_title"
        " FROM show WHERE tracking_space = 'anime'"
    ).fetchall()
    for show in shows:
        if show["score"] is not None:
            skipped_already_scored += 1
            continue
        season_rows = conn.execute(
            "SELECT anilist_id FROM season WHERE show_id = ? AND anilist_id IS NOT NULL",
            (show["id"],),
        ).fetchall()
        # the distinct LCARS-scale scores across this show's scored seasons
        values = sorted(
            {
                _to_lcars_score(anilist_scores[s["anilist_id"]])
                for s in season_rows
                if s["anilist_id"] in anilist_scores
            }
        )
        if not values:
            skipped_no_data += 1
            continue
        if len(values) > 1:
            divergent.append((show["id"], show["display_title"], values))
            continue
        show_score = values[0]
        conn.execute(
            "UPDATE show SET score = ?, updated_at = ? WHERE id = ?",
            (show_score, now, show["id"]),
        )
        conn.execute(
            "INSERT INTO score_change"
            " (id, show_id, previous_score, new_score, changed_at, changed_by)"
            " VALUES (?, ?, ?, ?, ?, 'anilist_import')",
            (ids.generate_id(conn, "o"), show["id"], None, show_score, now),
        )
        filled += 1
    conn.commit()
    return {
        "anime_shows": len(shows),
        "filled": filled,
        "skipped_already_scored": skipped_already_scored,
        "skipped_no_data": skipped_no_data,
        "divergent": divergent,
    }


def _run(db_path: Path, token: str) -> None:
    fmt = anilist_client.fetch_score_format(token)
    if fmt != _EXPECTED_SCORE_FORMAT:
        raise SystemExit(
            f"AniList account scoreFormat is {fmt!r}, not {_EXPECTED_SCORE_FORMAT!r}. "
            "Every score push/read in this codebase assumes POINT_100 (the '* 5' in "
            "_push_season_score) — this is a real bug to fix, not something to work around. "
            "Aborting before writing anything."
        )
    anilist_scores = _anilist_scores_by_id(token)
    print(f"AniList entries with a real score: {len(anilist_scores)}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    season_result = run_season_pass(conn, anilist_scores)
    print("--- season pass ---")
    for k, v in season_result.items():
        print(f"  {k}: {v}")

    show_result = run_show_pass(conn, anilist_scores)
    print("--- show pass ---")
    divergent = show_result.pop("divergent")
    for k, v in show_result.items():
        print(f"  {k}: {v}")
    print(f"  divergent (show.score left for manual): {len(divergent)}")
    for show_id, title, values in divergent:
        print(f"    {show_id} {title!r}: season scores {values}")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Path to the LCARS sqlite DB")
    parser.add_argument(
        "--token",
        help="AniList access token (default: whatever lcars.ini/env resolves to, like the server)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="Run for real against a throwaway copy, then discard"
    )
    mode.add_argument("--apply", action="store_true", help="Write to --db directly")
    args = parser.parse_args()

    config.set_current(config.load_config())
    token = args.token or config.get_current().anilist_access_token
    if not token:
        raise SystemExit(
            "No AniList access token — set one in lcars.ini (run `lcars anilist-login`) or pass "
            "--token. Run this on the production host where the token already lives."
        )

    if args.apply:
        _run(args.db, token)
        return

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch.db"
        shutil.copy(args.db, scratch)
        print(f"[dry run — operating on a throwaway copy: {scratch}]")
        _run(scratch, token)
        print("[dry run complete — throwaway copy discarded, --db untouched]")


if __name__ == "__main__":
    main()
