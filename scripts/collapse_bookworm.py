"""One-time data script — collapse Ascendance of a Bookworm's 4 sibling
show rows into one show with 4 seasons, 2026-08-27 (S4b, decision D2).

**Background**

Before this script: 4 LCARS show rows share tvdb_id 366263 (Sonarr's one
flat "Ascendance of a Bookworm" series), each holding its own Part as
season_number=1.  That shape was a workaround for the season-subdivision
problem (NEXT_UP.md D2, 2026-08-26): LCARS had no per-season AniList/MAL
mapping, so each part needed its own show to carry its own reconcile link.
Season subdivision (S1–S3) fixed the root cause: `season_external_id` now
holds per-season AniList/MAL ids independently of the show row.  The sibling
shape is no longer needed.

**What this script does**

Winner: s-2k4jb6 (Part 1, AniList 108268, already has tvdb/sonarr/anilist
show_external_id rows and season_number=1 with abs_start=1/abs_end=14).

Losers and their new season assignments:
  s-mzb7jx  Part 2  → season 2  (AniList 113693, abs 15-26)
  s-f732f2  Part 3  → season 3  (AniList 121176, abs 27-36)
  s-fwxa7m  Part 4  → season 4  (AniList 171110, abs 37-60)

Per-step actions (all in one deferred-FK transaction):

1. season rows    — UPDATE show_id → winner, season_number → 2/3/4
2. episode rows   — UPDATE show_id → winner, season → 2/3/4
3. watch_event    — UPDATE show_id → winner, season → 2/3/4
4. score_change / status_change — UPDATE show_id → winner (history follows)
5. show_synonym   — INSERT OR IGNORE loser synonyms onto winner (keeps
                    per-part titles searchable under the flat show)
6. show_studio    — INSERT OR IGNORE, skip winner duplicates
7. show_person    — INSERT rows not already present on winner
8. show_service_presence — DELETE loser rows (winner's own are up-to-date)
9. episode_numbering_mapping — DELETE loser rows (winner has its own)
10. show_relation —
      • INSERT (s-ebqx1f → winner) to replace dead-stub's link to Part 2
      • DELETE (s-ebqx1f → s-mzb7jx) old dead-stub link
      • DELETE all sibling-to-sibling relations (would become self-relations)
      • DELETE loser→OVA relations (winner already has winner→OVA)
11. show_external_id — DELETE loser rows (anilist/mal ids already in
                       season_external_id; winner keeps its own tvdb/sonarr)
12. show          — DELETE loser rows

Season 0 specials on winner (4 unwatched episodes): stay as-is; season_number=0
has NULL abs ranges by design and needs no range routing.

Display title: winner's existing title ("Ascendance of a Bookworm") stays.
Show-level AniList id: winner keeps 108268 (Part 1's id, already present).

Usage:
    python scripts/collapse_bookworm.py --db PATH [--dry-run]
    python scripts/collapse_bookworm.py --db PATH --apply
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lcars import util

# ---------------------------------------------------------------------------
# Hard-coded collapse config — one-time script, no general-purpose logic
# ---------------------------------------------------------------------------

WINNER = "s-2k4jb6"

# (loser_show_id, new_season_number)
LOSERS: list[tuple[str, int]] = [
    ("s-mzb7jx", 2),
    ("s-f732f2", 3),
    ("s-fwxa7m", 4),
]

LOSER_IDS = [lid for lid, _ in LOSERS]

# Dead-stub that linked to Part 2 — needs its relation updated to winner
DEAD_STUB = "s-ebqx1f"

# OVA show that winner already links to — loser's duplicate relation must be deleted
OVA = "s-5gbznf"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _report_current_state(conn: sqlite3.Connection) -> dict:
    """Gather counts for --dry-run output and pre-flight verification."""
    report: dict = {}

    for loser, season_num in LOSERS:
        eps = conn.execute(
            "SELECT COUNT(*) FROM episode WHERE show_id = ?", (loser,)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT COUNT(*) FROM watch_event WHERE show_id = ?", (loser,)
        ).fetchone()[0]
        season_row = conn.execute(
            "SELECT id, season_number, abs_start, abs_end FROM season WHERE show_id = ?",
            (loser,),
        ).fetchone()
        report[loser] = {
            "new_season": season_num,
            "episodes": eps,
            "watch_events": events,
            "season_id": season_row["id"] if season_row else None,
            "season_number_now": season_row["season_number"] if season_row else None,
            "abs_start": season_row["abs_start"] if season_row else None,
            "abs_end": season_row["abs_end"] if season_row else None,
        }

    report["_winner_seasons_before"] = conn.execute(
        "SELECT season_number FROM season WHERE show_id = ? ORDER BY season_number",
        (WINNER,),
    ).fetchall()

    report["_synonyms"] = conn.execute(
        "SELECT show_id, synonym FROM show_synonym"
        " WHERE show_id IN (?,?,?)"
        " ORDER BY show_id, synonym",
        LOSER_IDS,
    ).fetchall()

    report["_persons"] = conn.execute(
        "SELECT COUNT(*) FROM show_person WHERE show_id IN (?,?,?)", LOSER_IDS
    ).fetchone()[0]

    report["_studios"] = conn.execute(
        "SELECT COUNT(*) FROM show_studio WHERE show_id IN (?,?,?)", LOSER_IDS
    ).fetchone()[0]

    report["_relations"] = conn.execute(
        "SELECT show_id, related_show_id FROM show_relation"
        " WHERE show_id IN (?,?,?) OR related_show_id IN (?,?,?)",
        (*LOSER_IDS, *LOSER_IDS),
    ).fetchall()

    return report


def collapse(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Run the collapse.  In dry_run mode prints the plan and returns counts
    without committing; in apply mode commits the transaction."""
    now = util.now_utc_iso()
    report = _report_current_state(conn)

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------
    winner_exists = conn.execute(
        "SELECT 1 FROM show WHERE id = ?", (WINNER,)
    ).fetchone()
    if not winner_exists:
        print(f"ERROR: winner show {WINNER!r} not found in DB")
        sys.exit(1)

    for loser, _ in LOSERS:
        if not conn.execute("SELECT 1 FROM show WHERE id = ?", (loser,)).fetchone():
            print(f"ERROR: loser show {loser!r} not found in DB")
            sys.exit(1)
        season_count = conn.execute(
            "SELECT COUNT(*) FROM season WHERE show_id = ?", (loser,)
        ).fetchone()[0]
        if season_count != 1:
            print(
                f"ERROR: expected exactly 1 season for loser {loser!r},"
                f" found {season_count}"
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # Dry-run output
    # ------------------------------------------------------------------
    print("=== Bookworm collapse plan ===")
    print(f"Winner: {WINNER}")
    print()
    for loser, season_num in LOSERS:
        d = report[loser]
        print(
            f"  {loser}  season_number {d['season_number_now']} → {season_num}"
            f"  abs {d['abs_start']}–{d['abs_end']}"
            f"  ({d['episodes']} eps, {d['watch_events']} events)"
        )
    print()
    print(f"  show_synonym rows to move: {len(report['_synonyms'])}")
    for row in report["_synonyms"]:
        print(f"    {row['show_id']}: {row['synonym']!r}")
    print(f"  show_person rows to move: {report['_persons']}")
    print(f"  show_studio rows to move: {report['_studios']}")
    print()
    print("  show_relation changes:")
    for row in report["_relations"]:
        src, dst = row["show_id"], row["related_show_id"]
        # Classify
        both_losers = src in LOSER_IDS and dst in LOSER_IDS
        winner_loser = (src == WINNER and dst in LOSER_IDS) or (
            src in LOSER_IDS and dst == WINNER
        )
        dead_stub_to_loser = src == DEAD_STUB and dst in LOSER_IDS
        loser_to_ova = src in LOSER_IDS and dst == OVA
        if both_losers or winner_loser:
            print(f"    DELETE (sibling):       {src} → {dst}")
        elif dead_stub_to_loser:
            print(f"    UPDATE (dead-stub):     {src} → {dst}  becomes  {src} → {WINNER}")
        elif loser_to_ova:
            print(f"    DELETE (dup OVA link):  {src} → {dst}")
        else:
            print(f"    ??? (unexpected):       {src} → {dst}")

    if dry_run:
        print()
        print("Dry run — no changes made.")
        return report

    # ------------------------------------------------------------------
    # Apply (single transaction, deferred FK checks)
    # ------------------------------------------------------------------
    # BEGIN IMMEDIATE first, then PRAGMA — defer_foreign_keys only takes effect
    # when set inside an open transaction (same pattern show_merge.py documents
    # at line 180).
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA defer_foreign_keys = ON")

    try:
        for loser, season_num in LOSERS:
            # 1. season
            conn.execute(
                "UPDATE season SET show_id = ?, season_number = ?, updated_at = ?"
                " WHERE show_id = ?",
                (WINNER, season_num, now, loser),
            )
            # 2. episode
            conn.execute(
                "UPDATE episode SET show_id = ?, season = ?, updated_at = ?"
                " WHERE show_id = ?",
                (WINNER, season_num, now, loser),
            )
            # 3. watch_event
            conn.execute(
                "UPDATE watch_event SET show_id = ?, season = ?"
                " WHERE show_id = ?",
                (WINNER, season_num, loser),
            )
            # 4. score_change / status_change
            conn.execute(
                "UPDATE score_change SET show_id = ? WHERE show_id = ?",
                (WINNER, loser),
            )
            conn.execute(
                "UPDATE status_change SET show_id = ? WHERE show_id = ?",
                (WINNER, loser),
            )

        # 5. show_synonym — INSERT OR IGNORE preserves dedup via UNIQUE(show_id,synonym)
        conn.execute(
            "INSERT OR IGNORE INTO show_synonym (show_id, synonym, created_at)"
            " SELECT ?, synonym, created_at FROM show_synonym WHERE show_id IN (?,?,?)",
            (WINNER, *LOSER_IDS),
        )
        conn.execute(
            "DELETE FROM show_synonym WHERE show_id IN (?,?,?)", LOSER_IDS
        )

        # 6. show_studio — PRIMARY KEY (show_id, studio_id, role_type)
        conn.execute(
            "INSERT OR IGNORE INTO show_studio (show_id, studio_id, role_type)"
            " SELECT ?, studio_id, role_type FROM show_studio WHERE show_id IN (?,?,?)",
            (WINNER, *LOSER_IDS),
        )
        conn.execute(
            "DELETE FROM show_studio WHERE show_id IN (?,?,?)", LOSER_IDS
        )

        # 7. show_person — no UNIQUE constraint; deduplicate manually
        conn.execute(
            "INSERT INTO show_person (show_id, person_id, role_type, character_name)"
            " SELECT ?, sp.person_id, sp.role_type, sp.character_name"
            " FROM show_person sp"
            " WHERE sp.show_id IN (?,?,?)"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM show_person p2"
            "     WHERE p2.show_id = ? AND p2.person_id = sp.person_id"
            "       AND p2.role_type = sp.role_type"
            "   )",
            (WINNER, *LOSER_IDS, WINNER),
        )
        conn.execute(
            "DELETE FROM show_person WHERE show_id IN (?,?,?)", LOSER_IDS
        )

        # 8. show_service_presence — delete loser rows; winner has its own
        conn.execute(
            "DELETE FROM show_service_presence WHERE show_id IN (?,?,?)", LOSER_IDS
        )

        # 9. episode_numbering_mapping — winner already has its own row
        conn.execute(
            "DELETE FROM episode_numbering_mapping WHERE show_id IN (?,?,?)", LOSER_IDS
        )

        # 10. show_relation
        # Insert dead-stub → winner (replace dead-stub → Part 2)
        conn.execute(
            "INSERT OR IGNORE INTO show_relation (show_id, related_show_id, created_at)"
            " VALUES (?, ?, ?)",
            (DEAD_STUB, WINNER, now),
        )
        # Delete old dead-stub → loser links
        conn.execute(
            "DELETE FROM show_relation WHERE show_id = ? AND related_show_id IN (?,?,?)",
            (DEAD_STUB, *LOSER_IDS),
        )
        # Delete all remaining loser-touching relations (siblings + dup OVA links)
        conn.execute(
            "DELETE FROM show_relation"
            " WHERE show_id IN (?,?,?) OR related_show_id IN (?,?,?)",
            (*LOSER_IDS, *LOSER_IDS),
        )

        # 11. show_external_id — anilist/mal per-part ids are in season_external_id;
        #     tvdb/sonarr on losers are duplicates of winner's own rows
        conn.execute(
            "DELETE FROM show_external_id WHERE show_id IN (?,?,?)", LOSER_IDS
        )

        # 12. show — delete loser show rows (all FK children already moved)
        conn.execute(
            "DELETE FROM show WHERE id IN (?,?,?)", LOSER_IDS
        )

        conn.execute("COMMIT")

    except Exception:
        conn.execute("ROLLBACK")
        raise

    print()
    print("Applied — verifying...")
    seasons_after = conn.execute(
        "SELECT season_number, abs_start, abs_end FROM season WHERE show_id = ?"
        " ORDER BY season_number",
        (WINNER,),
    ).fetchall()
    print(f"Winner seasons after collapse ({len(seasons_after)}):")
    for s in seasons_after:
        print(f"  season {s['season_number']}  abs {s['abs_start']}–{s['abs_end']}")

    ep_count = conn.execute(
        "SELECT COUNT(*) FROM episode WHERE show_id = ?", (WINNER,)
    ).fetchone()[0]
    event_count = conn.execute(
        "SELECT COUNT(*) FROM watch_event WHERE show_id = ?", (WINNER,)
    ).fetchone()[0]
    print(f"Winner episode total: {ep_count}")
    print(f"Winner watch_event total: {event_count}")

    leftover_losers = conn.execute(
        "SELECT COUNT(*) FROM show WHERE id IN (?,?,?)", LOSER_IDS
    ).fetchone()[0]
    print(f"Loser show rows remaining: {leftover_losers} (expect 0)")

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", required=True, help="Path to lcars.db")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print plan without applying")
    mode.add_argument("--apply", action="store_true", help="Apply the collapse")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    collapse(conn, dry_run=args.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
