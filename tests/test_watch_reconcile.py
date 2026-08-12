"""B.15 — AniList -> LCARS watch reconciliation, live-caught 2026-08-12.
Same real-migrated-SQLite-DB approach test_show_merge.py already
established for exactly the same reason: this module's write pattern
(bulk watch_event backfill + episode.state + show.status correction)
is real enough that a mocked connection would hide the exact bugs it
exists to avoid.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, watch_reconcile


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "watch_reconcile_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture(autouse=True)
def _reset_config():
    config.set_current(config.Config())
    yield
    config.set_current(config.Config())


def _show(conn, show_id, title="Show", status="planned", tracked=1):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'romaji', ?, ?, 'x', 'x')",
        (show_id, title, status, tracked),
    )


def _season(conn, season_id, show_id, season_number, anilist_id):
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number, anilist_id),
    )


def _episode(conn, episode_id, show_id, season, episode, state="unwatched"):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, 'x', 'x')",
        (episode_id, show_id, season, episode, state),
    )


def _watch_event(conn, event_id, show_id, season, episode):
    conn.execute(
        "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
        " VALUES (?, ?, ?, ?, 'x', 'x')",
        (event_id, show_id, season, episode),
    )


def _configure_anilist(monkeypatch, entries: list[dict]):
    config.set_current(config.Config(anilist_access_token="tok"))
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token: entries)


def _entry(anilist_id, status="CURRENT", progress=0):
    return {"anilist_id": anilist_id, "status": status, "progress": progress, "format": "TV"}


def test_returns_all_zero_when_anilist_not_configured(conn):
    result = watch_reconcile.reconcile_watch_progress(conn)
    assert result == {
        "seasons_checked": 0,
        "not_matched_on_anilist": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
    }


def test_backfills_unwatched_episodes_up_to_anilist_progress(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _episode(conn, "e-episd1", "s-showw1", 1, 1)
    _episode(conn, "e-episd2", "s-showw1", 1, 2)
    _episode(conn, "e-episd3", "s-showw1", 1, 3)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=3)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["episodes_backfilled"] == 3
    assert result["seasons_checked"] == 1
    states = conn.execute(
        "SELECT episode, state FROM episode WHERE show_id = 's-showw1'"
    ).fetchall()
    assert all(row["state"] == "watched" for row in states)
    events = conn.execute("SELECT episode FROM watch_event WHERE show_id = 's-showw1'").fetchall()
    assert sorted(r["episode"] for r in events) == [1, 2, 3]


def test_never_touches_a_skipped_episode(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _episode(conn, "e-episd1", "s-showw1", 1, 1, state="unwatched")
    _episode(conn, "e-episd2", "s-showw1", 1, 2, state="skipped")
    _episode(conn, "e-episd3", "s-showw1", 1, 3, state="unwatched")
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=3)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    # Only the two genuinely-unwatched episodes backfilled — the deliberate skip is untouched.
    assert result["episodes_backfilled"] == 2
    ep2 = conn.execute("SELECT state FROM episode WHERE id = 'e-episd2'").fetchone()
    assert ep2["state"] == "skipped"
    assert conn.execute("SELECT * FROM watch_event WHERE episode = 2").fetchone() is None


def test_does_not_duplicate_an_already_watched_episode(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _episode(conn, "e-episd1", "s-showw1", 1, 1, state="watched")
    _watch_event(conn, "w-exist1", "s-showw1", 1, 1)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["episodes_backfilled"] == 0
    events = conn.execute("SELECT id FROM watch_event WHERE show_id = 's-showw1'").fetchall()
    assert len(events) == 1  # still just the original, no duplicate


def test_updates_show_status_when_it_diverges_from_anilist(conn, monkeypatch):
    _show(conn, "s-showw1", status="completed")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=0)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["shows_status_updated"] == 1
    row = conn.execute("SELECT status FROM show WHERE id = 's-showw1'").fetchone()
    assert row["status"] == "watching"
    change = conn.execute(
        "SELECT previous_status, new_status, changed_by FROM status_change"
        " WHERE show_id = 's-showw1'"
    ).fetchone()
    assert change["previous_status"] == "completed"
    assert change["new_status"] == "watching"
    assert change["changed_by"] == "anilist_reconcile"


def test_leaves_status_unchanged_and_writes_no_history_when_already_correct(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=0)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["shows_status_updated"] == 0
    assert conn.execute("SELECT * FROM status_change WHERE show_id = 's-showw1'").fetchone() is None


def test_multi_season_show_uses_the_highest_season_numbers_status(conn, monkeypatch):
    _show(conn, "s-showw1", status="completed")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _season(conn, "z-seasn2", "s-showw1", 2, anilist_id=200)
    conn.commit()
    # Season 1 finished (COMPLETED); season 2 (the current cour) is CURRENT —
    # the show-level status should follow season 2, not season 1.
    _configure_anilist(
        monkeypatch,
        [_entry(100, status="COMPLETED", progress=12), _entry(200, status="CURRENT", progress=2)],
    )

    watch_reconcile.reconcile_watch_progress(conn)

    row = conn.execute("SELECT status FROM show WHERE id = 's-showw1'").fetchone()
    assert row["status"] == "watching"


def test_maps_repeating_to_watching(conn, monkeypatch):
    _show(conn, "s-showw1", status="completed")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="REPEATING", progress=1)])

    watch_reconcile.reconcile_watch_progress(conn)

    row = conn.execute("SELECT status FROM show WHERE id = 's-showw1'").fetchone()
    assert row["status"] == "watching"


def test_counts_a_season_with_no_matching_anilist_entry(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=999)  # not on the fetched list below
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["not_matched_on_anilist"] == 1
    assert result["seasons_checked"] == 0


def test_ignores_seasons_with_no_anilist_id_at_all(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, source, created_at, updated_at)"
        " VALUES ('z-seasn1', 's-showw1', 1, 'manual', 'x', 'x')"
    )
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result == {
        "seasons_checked": 0,
        "not_matched_on_anilist": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
    }
