"""One-time backfill — AniList authoritative titles -> LCARS title fields,
2026-08-26 (the catalog-wide follow-up to the 2026-08-24 Bookworm Part 1
title-corruption fix; see that NEXT_UP entry for the audit).

**The problem** (audit, 2026-08-24): title fields are caller-input-at-
creation only — `metadata._fetch_anilist` fills poster/synopsis/etc on
every refresh but never touches titles, and nothing corrects them after
creation. Shows added before their AniList link existed (pre-A.20) can
therefore carry wrong/missing titles permanently. Sampling the 1457
`tracking_space='anime'` shows that have an AniList link but a NULL
`title_english` found three categories: ~22% (~300) *corrupted* —
`title_romaji` holds the real English string verbatim, AniList genuinely
has an English title LCARS never stored; ~47% (~700) simply *missing* an
English title AniList does have; ~32% (~470) legitimately titleless in
English on AniList (nothing to fix). The categories can't be told apart
by string heuristics — only a real per-show AniList re-fetch + compare.

**What this does**: for exactly that cohort (`tracking_space='anime'`,
`title_english IS NULL`, has an `anilist` link), batch-fetches AniList's
own `title { romaji english native }` (50 ids/request, same shape as
`backfill_synonyms.py`) and, **only when AniList actually has an English
title**, overwrites all three title fields with AniList's authoritative
values and sets `primary_title = 'english'`. A show AniList lists with no
English title is left completely untouched (the "legitimately titleless"
case — nothing to fix). `display_title_override` (a separate column) is
never touched, so a user's pinned display title survives. `primary_title`
is written in the same UPDATE as the titles, so the row-level CHECK
(`primary_title='english' AND title_english IS NOT NULL`) always sees the
final, consistent state.

Scoped conservatively to the NULL-`title_english` cohort — a show that
already has an English title is assumed correct and left alone (the audit
found no corruption class among those).

**Reuses the deployed image's own `anilist_client._graphql_request`**
(shared throttle + error handling); direct SQL, no mutation (a
client-facing `refreshTitlesFromAniList` mutation is a separate, still-
open NEXT_UP item — nothing here needs it). Idempotent: a second run
re-fetches and re-applies the same authoritative values (a no-op once
corrected). Dry run copies the DB to a throwaway and runs the real apply
path against it — the printed category counts are the audit
verification, so run --dry-run first and sanity-check the split (~300
corrupted / ~700 filled / ~470 titleless) before --apply.

Usage:
    python scripts/backfill_titles.py --db PATH --dry-run
    python scripts/backfill_titles.py --db PATH --apply
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lcars import anilist_client, config, util

_BATCH = 50
_TITLES_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) { id title { romaji english native } }
  }
}
"""


def _titles_by_id(anilist_ids: list[int]) -> dict[int, dict]:
    """{anilist_id -> {romaji, english, native}} in batches of 50."""
    result: dict[int, dict] = {}
    for start in range(0, len(anilist_ids), _BATCH):
        batch = anilist_ids[start : start + _BATCH]
        data = anilist_client._graphql_request(
            _TITLES_QUERY, {"ids": batch}, token=None, client=None
        )
        for media in data["Page"]["media"]:
            result[media["id"]] = media.get("title") or {}
        print(f"  fetched {min(start + _BATCH, len(anilist_ids))}/{len(anilist_ids)} ids")
    return result


def run(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT s.id AS show_id, s.title_romaji, e.external_id"
        " FROM show s JOIN show_external_id e ON e.show_id = s.id AND e.service = 'anilist'"
        " WHERE s.tracking_space = 'anime' AND s.title_english IS NULL"
    ).fetchall()
    cohort = [(r["show_id"], r["title_romaji"], int(r["external_id"])) for r in rows]
    unique_ids = sorted({aid for _, _, aid in cohort})
    print(f"cohort (anime, title_english NULL, anilist-linked): {len(cohort)}"
          f" ({len(unique_ids)} unique anilist ids)")

    titles_by_id = _titles_by_id(unique_ids)
    now = util.now_utc_iso()
    corrupted, filled, titleless, not_found = 0, 0, 0, 0
    for show_id, lcars_romaji, anilist_id in cohort:
        title = titles_by_id.get(anilist_id)
        if title is None:
            not_found += 1
            continue
        english = (title.get("english") or "").strip() or None
        if english is None:
            titleless += 1  # AniList itself has no English title — nothing to fix
            continue
        romaji = title.get("romaji")
        native = title.get("native")
        if lcars_romaji == english:
            corrupted += 1  # title_romaji was holding the English string
        else:
            filled += 1  # romaji was fine, just missing the English title
        conn.execute(
            "UPDATE show SET title_romaji = ?, title_english = ?,"
            " title_native = COALESCE(?, title_native), primary_title = 'english',"
            " updated_at = ? WHERE id = ?",
            (romaji, english, native, now, show_id),
        )
    conn.commit()
    return {
        "cohort": len(cohort),
        "corrupted_fixed": corrupted,
        "english_filled": filled,
        "titleless_left_untouched": titleless,
        "anilist_not_found": not_found,
    }


def _run(db_path: Path) -> None:
    config.set_current(config.load_config())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    result = run(conn)
    print("--- title backfill ---")
    for k, v in result.items():
        print(f"  {k}: {v}")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Path to the LCARS sqlite DB")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="Run for real against a throwaway copy, then discard"
    )
    mode.add_argument("--apply", action="store_true", help="Write to --db directly")
    args = parser.parse_args()

    if args.apply:
        _run(args.db)
        return

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch.db"
        shutil.copy(args.db, scratch)
        print(f"[dry run — operating on a throwaway copy: {scratch}]")
        _run(scratch)
        print("[dry run complete — throwaway copy discarded, --db untouched]")


if __name__ == "__main__":
    main()
