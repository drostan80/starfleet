"""One-time backfill — AniList `Media.synonyms` -> LCARS `show_synonym`,
2026-08-26 (companion to the synonyms feature shipped v0.1.36).

`show_synonym` is go-forward-capture: a show only gains synonyms on its
next real AniList fetch (`refreshShowMetadata` or a scheduled pass). This
populates every already-tracked, AniList-linked show at once instead of
waiting for each to be refreshed — user's request ("LCARS must be
populated with the synonyms from anilist").

**Batched, not per-show**: AniList's `Media` accepts an `id_in` filter,
so this fetches synonyms 50 ids per request (~47 requests for ~2300
shows) via `Page(perPage:50){media(id_in:[...])}` rather than one call
each. Unauthenticated — synonyms are public (same as `fetch_media`).

**Reuses the deployed image's own code**: calls
`anilist_client._graphql_request` (the shared throttle + error handling
every AniList call in this codebase goes through) and
`metadata._sync_synonyms` (the exact delete-then-insert the live
`_fetch_anilist` path uses), so this writes byte-for-byte what a real
refresh would. No new app code — just the batching query, which the
normal single-id fetch has no need for.

**Idempotent**: `_sync_synonyms` is delete-then-insert per show, so a
re-run simply re-syncs. A show whose AniList entry returns no synonyms
(or is gone) ends with an empty synonym set, same as a live refresh.

**Dry run** copies the DB to a throwaway file and runs the real
`--apply` path against it (still makes the live AniList reads, which have
no side effects), same shape as `import_trakt_history.py`/
`import_anilist_scores.py`.

Usage:
    python scripts/backfill_synonyms.py --db PATH --dry-run
    python scripts/backfill_synonyms.py --db PATH --apply
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lcars import anilist_client, config, metadata, util

_BATCH = 50
_SYNONYMS_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) { id synonyms }
  }
}
"""


def _synonyms_by_id(anilist_ids: list[int]) -> dict[int, list[str]]:
    """{anilist_id -> synonyms} for every id, fetched in batches of 50.
    An id AniList no longer knows just doesn't come back — the caller
    treats a missing id as an empty synonym list, same net effect as a
    live refresh of a since-deleted entry."""
    result: dict[int, list[str]] = {}
    for start in range(0, len(anilist_ids), _BATCH):
        batch = anilist_ids[start : start + _BATCH]
        data = anilist_client._graphql_request(
            _SYNONYMS_QUERY, {"ids": batch}, token=None, client=None
        )
        for media in data["Page"]["media"]:
            result[media["id"]] = media.get("synonyms") or []
        print(f"  fetched {min(start + _BATCH, len(anilist_ids))}/{len(anilist_ids)} ids")
    return result


def run(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT show_id, external_id FROM show_external_id WHERE service = 'anilist'"
    ).fetchall()
    links = [(r["show_id"], int(r["external_id"])) for r in rows]
    unique_ids = sorted({aid for _, aid in links})
    print(f"anilist-linked shows: {len(links)} ({len(unique_ids)} unique anilist ids)")

    synonyms_by_id = _synonyms_by_id(unique_ids)
    now = util.now_utc_iso()
    shows_with_synonyms, shows_empty, synonym_rows = 0, 0, 0
    for show_id, anilist_id in links:
        synonyms = synonyms_by_id.get(anilist_id, [])
        metadata._sync_synonyms(conn, show_id, synonyms, now)
        # count what _sync_synonyms actually wrote (deduped/blank-stripped)
        written = conn.execute(
            "SELECT COUNT(*) c FROM show_synonym WHERE show_id = ?", (show_id,)
        ).fetchone()["c"]
        if written:
            shows_with_synonyms += 1
            synonym_rows += written
        else:
            shows_empty += 1
    conn.commit()
    return {
        "shows_processed": len(links),
        "shows_with_synonyms": shows_with_synonyms,
        "shows_with_none": shows_empty,
        "synonym_rows_written": synonym_rows,
    }


def _run(db_path: Path) -> None:
    config.set_current(config.load_config())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    result = run(conn)
    print("--- backfill ---")
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
