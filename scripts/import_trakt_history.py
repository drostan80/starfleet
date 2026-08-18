"""One-time historical import — Trakt export -> LCARS, 2026-08-18
(NEXT_UP.md's PC.2, TV-history half only; AniList/MAL are a separate,
still-open half of that same line).

**Why this exists as a script, not a mutation**: PC.2 itself calls this
a "one-time historical import" — it runs once, against one specific
export file shape, for one person's data. Every other write path in
this codebase is a tested GraphQL mutation because it's meant to be
called again and again by real clients; this isn't. Reuses
`shows.create_show()` (the same show-creation path `addShow` itself
uses — real metadata fetch included, not a bare stub) directly, no
GraphQL round trip at all.

**Input**: a Trakt "export your data" zip (Settings > Data on Trakt's
own site) — read directly via `zipfile`, no unzip needed. Only the
files this actually uses: `watched-shows-{1,2}.json` (play counts,
show ids/title/year), `lists-list-*-{watching,paused,completed,
dropped,planned}.json` (status), `watched-history-*.json` (the real
per-episode watch events with real timestamps). `watched-movies.json`
is untouched — this user's export has none, and PC.2 always scoped this
to "TV shows."

**Why direct SQL for episodes/watch_event, not `addWatchEvent`/
`setStatus`**: those resolvers exist for one interactive call at a
time and carry real side effects meant for that (AniList status/score
push, `_stamp_season_started_at`, `_try_complete_season`/
`_try_complete_show` auto-completion, `_push_show_episode_progress`).
Routing ~13,600 historical events through them would mean firing all
of that per event — for any show this import matches against an
existing `tracking_space = 'anime'` LCARS show, that's a real risk of
spamming the user's actual AniList account with thousands of
plausible-looking but wrong pushes. This script writes `show`/
`show_external_id`/`status_change`/`episode`/`watch_event` rows
directly and nothing else — no AniList call is possible from this
script at all, regardless of a show's classification. (`create_show()`
itself still does its own real, single, best-effort metadata fetch per
new show — that part IS the normal add-show path, unchanged.)

**FK reality forced episode synthesis**: `watch_event(show_id, season,
episode)` is a composite foreign key into `episode` — confirmed via
`.schema watch_event` before writing a line of this, not assumed. A
show Sonarr never indexed (most of this export, live-measured: 366 of
429 shows) has zero episode rows, so a watch event for it needs one
synthesized first — `kind='special'` when `season == 0` (Sonarr's own
specials-bucket convention, matched, not guessed), 'regular'
otherwise, `state='watched'` immediately (we're writing this exact
show's own watch event in the same pass).

**Two independent passes, status then events** (advisor-caught before
writing any code): status is 429 single-field writes, trivially
verifiable by eye; the event pass is ~13,600 inserts with real
historical timestamps. Kept genuinely separate — a `--only status` /
`--only events` run scopes to just one — so a bad event-pass run can be
investigated/restored from a snapshot without touching the (already
verified-correct) status writes.

**Excluded from both passes**: Sword Art Online — the one Trakt show
that matched an existing LCARS show already `tracking_space = 'anime'`
(live-checked, not assumed) — confirmed with the user 2026-08-18: its
LCARS history should come from AniList, not Trakt, so this script
leaves it completely untouched.

**tracking_space for the 366 new shows**: 'tv' for all of them except
Cyberpunk: Edgerunners, the one show (of all 366) that resolves in the
same Fribb dataset the tvdb_id backfill already uses (`fribb.
resolve_season_candidate`, forward tvdb->anilist direction) — confirmed
live, not guessed, and confirmed with the user. Given an `anilist_id`
directly (120377), so `create_show()`'s own on-demand fetch picks up
the real AniList link immediately rather than needing a later `t` +
`refreshShowMetadata` correction.

**"Monster Eater" special case**: the one show with real play history
but no membership in any of the five status lists — confirmed with the
user 2026-08-18: `completed`.

**Idempotent by construction, not by a separate checkpoint**: re-running
is safe — `create_show()` itself refuses to double-create (`find_existing_
show`), the status pass only writes when `show.status` actually differs
from the desired one, `episode` rows use `INSERT OR IGNORE` against the
real `UNIQUE(show_id, season, episode)` constraint, and the watch_event
pass skips any (show_id, season, episode) that already has a
watch_event row — real, live-measured: every one of the 13,601 events
in this specific export is already unique per (show, season, episode),
so this dedup key doesn't discard genuine rewatch history for this
particular import (checked, not assumed — a different export with real
Trakt rewatch entries would need a real per-timestamp dedup instead).

**Dry run is a real scratch-copy run, not parallel read-only logic**:
an earlier draft tried to mirror the write logic with read-only checks
(SELECT instead of INSERT) so --dry-run wouldn't touch the database —
caught before ever running it: the events pass depends on shows the
status pass creates, so a truly side-effect-free "preview" of the
events pass would see none of those shows yet and wrongly report
almost all of them as unresolvable. --dry-run now copies the target db
to a temp file, runs the real --apply code path against the copy, and
discards it — the report is byte-for-byte what a real run would do,
not an approximation that can drift from it.

Usage:
    python scripts/import_trakt_history.py --db PATH --zip PATH --dry-run
    python scripts/import_trakt_history.py --db PATH --zip PATH --apply [--only status|events]
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lcars import config, ids, shows, util

STATUS_FILES = {
    "watching": "lists-list-36014483-watching.json",
    "paused": "lists-list-36014484-paused.json",
    "completed": "lists-list-36014485-completed.json",
    "dropped": "lists-list-36014486-dropped.json",
    "planned": "lists-list-36022621-planned.json",
}

EXCLUDED_TITLES = {"Sword Art Online"}  # confirmed with the user, 2026-08-18
MONSTER_EATER_STATUS = "completed"  # confirmed with the user, 2026-08-18
ANIME_OVERRIDE = {  # title -> anilist_id, confirmed live via Fribb + the user, 2026-08-18
    "Cyberpunk: Edgerunners": 120377,
}


def _load(z: zipfile.ZipFile, name: str) -> list[dict]:
    return json.loads(z.read(name))


def _unified_registry(z: zipfile.ZipFile) -> dict[int, dict]:
    """trakt_id -> {title, year, ids, aired_episodes, plays} for every
    show seen anywhere (watched-shows or a status list)."""
    registry: dict[int, dict] = {}
    for fname in ("watched-shows-1.json", "watched-shows-2.json"):
        for entry in _load(z, fname):
            s = entry["show"]
            registry[s["ids"]["trakt"]] = {
                "title": s["title"],
                "year": s.get("year"),
                "ids": s["ids"],
                "aired_episodes": s.get("aired_episodes"),
                "plays": entry["plays"],
            }
    for fname in STATUS_FILES.values():
        for entry in _load(z, fname):
            s = entry["show"]
            registry.setdefault(
                s["ids"]["trakt"],
                {
                    "title": s["title"],
                    "year": s.get("year"),
                    "ids": s["ids"],
                    "aired_episodes": s.get("aired_episodes"),
                    "plays": 0,
                },
            )
    return registry


def _status_by_trakt(z: zipfile.ZipFile) -> dict[int, str]:
    result: dict[int, str] = {}
    for status, fname in STATUS_FILES.items():
        for entry in _load(z, fname):
            result[entry["show"]["ids"]["trakt"]] = status
    return result


def _find_existing_show_id(conn: sqlite3.Connection, trakt_ids: dict) -> str | None:
    for service, key in (("tvdb", "tvdb"), ("tmdb", "tmdb"), ("imdb", "imdb")):
        value = trakt_ids.get(key)
        if value is None:
            continue
        row = conn.execute(
            "SELECT show_id FROM show_external_id WHERE service = ? AND external_id = ?",
            (service, str(value)),
        ).fetchone()
        if row is not None:
            return row["show_id"]
    return None


def _resolve_status(trakt_id: int, title: str, status_by_trakt: dict[int, str]) -> str:
    if title == "Monster Eater":
        return MONSTER_EATER_STATUS
    return status_by_trakt[trakt_id]  # every other show is in exactly one list — checked live


def run_status_pass(conn: sqlite3.Connection, z: zipfile.ZipFile) -> dict:
    registry = _unified_registry(z)
    status_by_trakt = _status_by_trakt(z)
    created, matched, status_changed, skipped_excluded, skipped_unchanged = 0, 0, 0, 0, 0

    for trakt_id, s in registry.items():
        if s["title"] in EXCLUDED_TITLES:
            skipped_excluded += 1
            continue
        desired_status = _resolve_status(trakt_id, s["title"], status_by_trakt)

        show_id = _find_existing_show_id(conn, s["ids"])
        if show_id is None:
            input_ = {
                "media_shape": "episodic",
                "tracking_space": "anime" if s["title"] in ANIME_OVERRIDE else "tv",
                "primary_title": "english",
                "title_english": s["title"],
                "tvdb_id": s["ids"].get("tvdb"),
                "tmdb_id": s["ids"].get("tmdb"),
                "imdb_id": s["ids"].get("imdb"),
            }
            if s["title"] in ANIME_OVERRIDE:
                input_["anilist_id"] = ANIME_OVERRIDE[s["title"]]
            show_id = shows.create_show(conn, input_)
            created += 1
        else:
            matched += 1

        row = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        if row["status"] == desired_status:
            skipped_unchanged += 1
            continue
        now = util.now_utc_iso()
        conn.execute(
            "UPDATE show SET status = ?, updated_at = ? WHERE id = ?",
            (desired_status, now, show_id),
        )
        conn.execute(
            "INSERT INTO status_change"
            " (id, show_id, previous_status, new_status, changed_at, changed_by)"
            " VALUES (?, ?, ?, ?, ?, 'trakt_import')",
            (ids.generate_id(conn, "c"), show_id, row["status"], desired_status, now),
        )
        status_changed += 1
        conn.commit()

    return {
        "shows_seen": len(registry),
        "created": created,
        "matched": matched,
        "status_changed": status_changed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_excluded": skipped_excluded,
    }


def run_events_pass(conn: sqlite3.Connection, z: zipfile.ZipFile) -> dict:
    episodes_created, events_written, events_skipped_dup, events_skipped_no_show = 0, 0, 0, 0
    history_files = sorted(n for n in z.namelist() if n.startswith("watched-history-"))
    now = util.now_utc_iso()

    for fname in history_files:
        for entry in _load(z, fname):
            show_ids = entry["show"]["ids"]
            title = entry["show"]["title"]
            if title in EXCLUDED_TITLES:
                continue
            season = entry["episode"]["season"]
            number = entry["episode"]["number"]
            watched_at = entry["watched_at"]
            ep_title = entry["episode"].get("title")

            show_id = _find_existing_show_id(conn, show_ids)
            if show_id is None:
                # Only happens if the status pass wasn't run first (--only events
                # on a show it never created) — real, reported, not silently skipped.
                events_skipped_no_show += 1
                continue

            existing_event = conn.execute(
                "SELECT 1 FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?",
                (show_id, season, number),
            ).fetchone()
            if existing_event is not None:
                events_skipped_dup += 1
                continue

            episode_exists = conn.execute(
                "SELECT 1 FROM episode WHERE show_id = ? AND season = ? AND episode = ?",
                (show_id, season, number),
            ).fetchone()
            if episode_exists is None:
                conn.execute(
                    "INSERT OR IGNORE INTO episode"
                    " (id, show_id, season, episode, kind, title, state, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'watched', ?, ?)",
                    (
                        ids.generate_id(conn, "e"),
                        show_id,
                        season,
                        number,
                        "special" if season == 0 else "regular",
                        ep_title,
                        now,
                        now,
                    ),
                )
                episodes_created += 1
            else:
                conn.execute(
                    "UPDATE episode SET state = 'watched', updated_at = ?"
                    " WHERE show_id = ? AND season = ? AND episode = ?",
                    (now, show_id, season, number),
                )

            conn.execute(
                "INSERT INTO watch_event"
                " (id, show_id, season, episode, watched_at, platform, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'trakt_import', ?)",
                (ids.generate_id(conn, "w"), show_id, season, number, watched_at, now),
            )
            events_written += 1

        conn.commit()

    return {
        "episodes_created": episodes_created,
        "events_written": events_written,
        "events_skipped_dup": events_skipped_dup,
        "events_skipped_no_show": events_skipped_no_show,
    }


def _run(db_path: Path, zip_path: Path, only: str | None) -> None:
    # Real lcars.ini (same default CONFIG_PATH cli.py itself uses) if one
    # exists on this host, else load_config()'s own "not configured" default
    # — fetch_and_populate's Sonarr/TMDB/AniList calls read whichever this
    # resolves to, so running this on the actual production host (where
    # lcars.ini has real credentials) is what makes new shows get real
    # metadata rather than a bare stub.
    config.set_current(config.load_config())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    z = zipfile.ZipFile(zip_path)

    if only in (None, "status"):
        result = run_status_pass(conn, z)
        print("--- status pass ---")
        for k, v in result.items():
            print(f"  {k}: {v}")

    if only in (None, "events"):
        result = run_events_pass(conn, z)
        print("--- events pass ---")
        for k, v in result.items():
            print(f"  {k}: {v}")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Path to the LCARS sqlite DB")
    parser.add_argument("--zip", required=True, type=Path, help="Path to the Trakt export zip")
    parser.add_argument(
        "--only", choices=["status", "events"], help="Run only one pass (default: both)"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="Run for real against a throwaway copy, then discard"
    )
    mode.add_argument("--apply", action="store_true", help="Write to --db directly")
    args = parser.parse_args()

    if args.apply:
        _run(args.db, args.zip, args.only)
        return

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch.db"
        shutil.copy(args.db, scratch)
        print(f"[dry run — operating on a throwaway copy: {scratch}]")
        _run(scratch, args.zip, args.only)
        print("[dry run complete — throwaway copy discarded, --db untouched]")


if __name__ == "__main__":
    main()
