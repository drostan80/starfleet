"""One-time backfill — season absolute-episode ranges + season_external_id
mirror, 2026-08-27 (Slice 2 of hierarchical season subdivision; see
NEXT_UP / design note artifact 01f25b72).

**What this does** (two independent, both inert-until-S3 parts):

1. **Range population (D4):** for every season of every anime show, fills
   `season.abs_start` / `season.abs_end` from observed integer
   `episode.absolute_number` values (MIN/MAX per season) — the real data,
   not accumulated counts. Seasons whose episodes have only synthesized
   (fractional) absolute numbers get NULL ranges + a report line — the
   honest answer when no source-reported positions exist. Season 0
   (specials bucket) is always skipped.

2. **External-id mirror:** copies existing `season.anilist_id` /
   `season.mal_id` into `season_external_id` rows so S3's reconcile
   read-switch has data to land on. This is a copy-forward, not a
   semantic change — the same ids, same rows, just in the range-based
   mapping table.

Both are independently deployable and behaviourally inert: **nothing reads
`abs_start`/`abs_end` or `season_external_id` yet** (S3 migrates the
reconcile reads; S4 does routing).

**Validation report** (printed, not pending_review rows):

- **Numbering gaps**: per season, checks that `abs_end - abs_start + 1`
  equals the observed episode count — a mismatch means gaps or extras in
  the absolute numbering within that season.
- **AniList width validation** (D4 primary): batch-fetches AniList's own
  per-entry `episodes` count and compares to range width. A mismatch
  flags the Mushoku Tensei / Fire Force shape — one AniList entry
  spanning multiple LCARS seasons (subdivision trigger), or a wrong
  AniList match.
- **Range integrity**: per show, seasons' ranges should be ascending,
  non-overlapping, and gap-free, and no episode's integer
  `absolute_number` should land inside a *different* season's range.

All failures are findings for human review before S3 ships.

**Bookworm note:** the four sibling shows' `absolute_number` values hold
Sonarr's continuous numbering (episode 55 → this part's episode 19).
Ranges derived per-sibling are in the post-collapse numbering space and
stay valid through S4 — S4 collapses the shows, it does not re-derive
ranges.

Usage:
    python scripts/backfill_season_ranges.py --db PATH --dry-run
    python scripts/backfill_season_ranges.py --db PATH --apply
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lcars import config, season_ranges, util

# ---------------------------------------------------------------------------
# Core logic — also importable by tests
# ---------------------------------------------------------------------------


def compute_season_ranges(conn: sqlite3.Connection) -> dict:
    """Compute and write abs_start/abs_end + mirror season_external_id.

    Returns a report dict with counts and any validation findings.
    """
    now = util.now_utc_iso()
    report = {
        "seasons_total": 0,
        "ranges_set": 0,
        "ranges_skipped_no_integer_abs": 0,
        "ranges_skipped_season_zero": 0,
        "external_ids_mirrored": 0,
        "numbering_gaps": [],
        "anilist_width_mismatches": [],
        "integrity_failures": [],
    }

    # -- Part 1: Range population ------------------------------------------

    # All anime seasons (join through show to get tracking_space)
    seasons = conn.execute(
        "SELECT s.id, s.show_id, s.season_number, s.anilist_id, s.mal_id"
        " FROM season s JOIN show sh ON sh.id = s.show_id"
        " WHERE sh.tracking_space = 'anime'"
        " ORDER BY s.show_id, s.season_number"
    ).fetchall()
    report["seasons_total"] = len(seasons)

    for season in seasons:
        season_id = season["id"]
        show_id = season["show_id"]
        season_number = season["season_number"]

        # Skip season 0 — specials have no meaningful absolute numbering
        if season_number == 0:
            report["ranges_skipped_season_zero"] += 1
            continue

        # MIN/MAX of *integer* absolute_number values only (exclude
        # synthesized fractional values — those carry a fractional part
        # deliberately, per _synthesize_absolute_numbers)
        range_row = conn.execute(
            "SELECT MIN(absolute_number) AS abs_min, MAX(absolute_number) AS abs_max,"
            "       COUNT(*) AS ep_count"
            " FROM episode"
            " WHERE show_id = ? AND season = ?"
            "   AND absolute_number IS NOT NULL"
            "   AND absolute_number = CAST(absolute_number AS INTEGER)",
            (show_id, season_number),
        ).fetchone()

        if range_row["ep_count"] == 0:
            report["ranges_skipped_no_integer_abs"] += 1
            continue

        abs_start = int(range_row["abs_min"])
        abs_end = int(range_row["abs_max"])

        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ? WHERE id = ?",
            (abs_start, abs_end, now, season_id),
        )
        report["ranges_set"] += 1

    conn.commit()

    # -- Part 2: External-id mirror ----------------------------------------

    for season in seasons:
        season_id = season["id"]
        anilist_id = season["anilist_id"]
        mal_id = season["mal_id"]

        if anilist_id is not None:
            conn.execute(
                "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                " VALUES (?, 'anilist', ?, ?)"
                " ON CONFLICT (season_id, service)"
                " DO UPDATE SET external_id = excluded.external_id",
                (season_id, anilist_id, now),
            )
            report["external_ids_mirrored"] += 1

        if mal_id is not None:
            conn.execute(
                "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                " VALUES (?, 'mal', ?, ?)"
                " ON CONFLICT (season_id, service)"
                " DO UPDATE SET external_id = excluded.external_id",
                (season_id, mal_id, now),
            )
            report["external_ids_mirrored"] += 1

    conn.commit()

    # -- Part 3: Validation ------------------------------------------------
    report["numbering_gaps"] = _check_numbering_gaps(conn)
    report["integrity_failures"] = _check_range_integrity(conn)

    return report


def _check_numbering_gaps(conn: sqlite3.Connection) -> list[dict]:
    """Check: abs_end - abs_start + 1 should match the count of episodes with
    integer absolute_number in that season. A mismatch means gaps or extras
    in the absolute numbering *within* the season — not an AniList issue,
    just the episode data itself.
    """
    mismatches = []
    rows = conn.execute(
        "SELECT s.id, s.show_id, s.season_number, s.anilist_id,"
        "       s.abs_start, s.abs_end,"
        "       sh.title_romaji"
        " FROM season s"
        " JOIN show sh ON sh.id = s.show_id"
        " WHERE s.abs_start IS NOT NULL AND s.abs_end IS NOT NULL"
    ).fetchall()
    for r in rows:
        range_width = r["abs_end"] - r["abs_start"] + 1
        ep_count = conn.execute(
            "SELECT COUNT(*) AS c FROM episode"
            " WHERE show_id = ? AND season = ?"
            "   AND absolute_number IS NOT NULL"
            "   AND absolute_number = CAST(absolute_number AS INTEGER)",
            (r["show_id"], r["season_number"]),
        ).fetchone()["c"]
        if range_width != ep_count:
            mismatches.append({
                "show": r["title_romaji"],
                "season": r["season_number"],
                "range": f"[{r['abs_start']}, {r['abs_end']}]",
                "range_width": range_width,
                "episode_count": ep_count,
                "note": "range width != observed episode count (gaps or extras in numbering)",
            })
    return mismatches


# _fetch_anilist_episode_counts ported to src/lcars/season_ranges.py (S5,
# 2026-08-27) so the live server can call it too.  Import from there to avoid
# a duplicate definition — this script is now a thin wrapper around the
# shared module function.
_fetch_anilist_episode_counts = season_ranges._fetch_anilist_episode_counts


def check_anilist_width(conn: sqlite3.Connection) -> list[dict]:
    """D4 primary validation: compare each season's range width to its
    AniList entry's episode count.

    A mismatch flags the Mushoku Tensei / Fire Force shape — one AniList
    entry covering more episodes than one LCARS season holds — or a wrong
    AniList match.  Both need human review before S3.
    """
    rows = conn.execute(
        "SELECT s.id, s.show_id, s.season_number, s.anilist_id,"
        "       s.abs_start, s.abs_end,"
        "       sh.title_romaji"
        " FROM season s"
        " JOIN show sh ON sh.id = s.show_id"
        " WHERE s.abs_start IS NOT NULL AND s.abs_end IS NOT NULL"
        "   AND s.anilist_id IS NOT NULL"
        " ORDER BY sh.title_romaji, s.season_number"
    ).fetchall()
    if not rows:
        return [], []

    unique_ids = sorted({r["anilist_id"] for r in rows})
    print(f"  checking {len(rows)} seasons against"
          f" {len(unique_ids)} unique AniList entries...")
    anilist_counts = _fetch_anilist_episode_counts(unique_ids)

    # Separate "AniList returned no media for this id" (dead/wrong link)
    # from "AniList says episodes is null" (airing, correctly skipped).
    requested = set(unique_ids)
    returned = set(anilist_counts.keys())
    not_found = sorted(requested - returned)

    mismatches = []
    for r in rows:
        anilist_eps = anilist_counts.get(r["anilist_id"])
        if anilist_eps is None:
            continue  # null episodes (airing) or not found — handled separately
        range_width = r["abs_end"] - r["abs_start"] + 1
        if range_width != anilist_eps:
            mismatches.append({
                "show": r["title_romaji"],
                "season": r["season_number"],
                "anilist_id": r["anilist_id"],
                "range": f"[{r['abs_start']}, {r['abs_end']}]",
                "range_width": range_width,
                "anilist_episodes": anilist_eps,
                "note": (
                    "range width != AniList episode count"
                    f" (anilist says {anilist_eps},"
                    f" range covers {range_width})"
                ),
            })
    return mismatches, not_found


def _check_range_integrity(conn: sqlite3.Connection) -> list[dict]:
    """Per show: seasons with ranges should have ascending, non-overlapping
    ranges, and no episode's integer absolute_number should land inside
    a different season's range.
    """
    failures = []

    # Get all shows that have at least one season with ranges
    show_ids = conn.execute(
        "SELECT DISTINCT show_id FROM season WHERE abs_start IS NOT NULL"
    ).fetchall()

    for show_row in show_ids:
        show_id = show_row["show_id"]
        title_row = conn.execute(
            "SELECT title_romaji FROM show WHERE id = ?", (show_id,)
        ).fetchone()
        title = title_row["title_romaji"] if title_row else show_id

        # All seasons with ranges for this show, ordered by season_number
        ranges = conn.execute(
            "SELECT id, season_number, abs_start, abs_end FROM season"
            " WHERE show_id = ? AND abs_start IS NOT NULL"
            " ORDER BY season_number",
            (show_id,),
        ).fetchall()

        # Check ascending + non-overlapping
        for i in range(1, len(ranges)):
            prev = ranges[i - 1]
            curr = ranges[i]
            if curr["abs_start"] <= prev["abs_end"]:
                failures.append({
                    "show": title,
                    "type": "overlap",
                    "detail": (
                        f"season {prev['season_number']}"
                        f" [{prev['abs_start']},{prev['abs_end']}]"
                        f" overlaps season {curr['season_number']}"
                        f" [{curr['abs_start']},{curr['abs_end']}]"
                    ),
                })
            elif curr["abs_start"] != prev["abs_end"] + 1:
                failures.append({
                    "show": title,
                    "type": "gap",
                    "detail": (
                        f"gap between season {prev['season_number']} (ends {prev['abs_end']})"
                        f" and season {curr['season_number']} (starts {curr['abs_start']})"
                    ),
                })

        # Check no episode's integer absolute_number crosses season boundaries
        for r in ranges:
            cross_check = conn.execute(
                "SELECT e.season, e.episode, e.absolute_number FROM episode e"
                " WHERE e.show_id = ? AND e.season != ?"
                "   AND e.absolute_number IS NOT NULL"
                "   AND e.absolute_number = CAST(e.absolute_number AS INTEGER)"
                "   AND CAST(e.absolute_number AS INTEGER) >= ?"
                "   AND CAST(e.absolute_number AS INTEGER) <= ?",
                (show_id, r["season_number"], r["abs_start"], r["abs_end"]),
            ).fetchall()
            for ep in cross_check:
                failures.append({
                    "show": title,
                    "type": "cross_boundary",
                    "detail": (
                        f"episode S{ep['season']}E{ep['episode']}"
                        f" (absolute {int(ep['absolute_number'])})"
                        f" lands inside season {r['season_number']}'s range"
                        f" [{r['abs_start']},{r['abs_end']}]"
                    ),
                })

    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run(db_path: Path) -> None:
    config.set_current(config.load_config())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    report = compute_season_ranges(conn)

    # AniList width validation — the D4 primary check
    print("  running AniList width validation...")
    anilist_mismatches, anilist_not_found = check_anilist_width(conn)
    report["anilist_width_mismatches"] = anilist_mismatches
    report["anilist_not_found"] = anilist_not_found

    print("--- season range backfill ---")
    print(f"  seasons total:                {report['seasons_total']}")
    print(f"  ranges set:                   {report['ranges_set']}")
    print(f"  skipped (no integer abs):     {report['ranges_skipped_no_integer_abs']}")
    print(f"  skipped (season 0):           {report['ranges_skipped_season_zero']}")
    print(f"  external ids mirrored:        {report['external_ids_mirrored']}")
    if report["numbering_gaps"]:
        print(f"\n  ⚠ numbering gaps ({len(report['numbering_gaps'])}):")
        for m in report["numbering_gaps"]:
            print(f"    {m['show']} S{m['season']}: {m['range']}"
                  f" width={m['range_width']} episodes={m['episode_count']}")
    if report["anilist_not_found"]:
        print(f"\n  ⚠ AniList ids not found ({len(report['anilist_not_found'])}):")
        print(f"    {report['anilist_not_found']}")
    if report["anilist_width_mismatches"]:
        n = len(report["anilist_width_mismatches"])
        print(f"\n  ⚠ AniList width mismatches ({n}):")
        for m in report["anilist_width_mismatches"]:
            print(f"    {m['show']} S{m['season']}: {m['range']}"
                  f" width={m['range_width']}"
                  f" anilist={m['anilist_episodes']}"
                  f" (anilist_id={m['anilist_id']})")
    if report["integrity_failures"]:
        print(f"\n  ⚠ integrity failures ({len(report['integrity_failures'])}):")
        for f in report["integrity_failures"]:
            print(f"    [{f['type']}] {f['show']}: {f['detail']}")
    has_issues = (
        report["numbering_gaps"]
        or report["anilist_width_mismatches"]
        or report["integrity_failures"]
    )
    if not has_issues:
        print("\n  ✓ all ranges valid — no gaps, mismatches, or integrity failures")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Path to the LCARS sqlite DB")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Run for real against a throwaway copy, then discard",
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
