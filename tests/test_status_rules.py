"""Status engine (PLAN-CODE phase 4) — RULEBOOK R2.7, R2.13–R2.19."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import status_rules

NOW = "2026-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "status.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, created_at, updated_at)"
        " VALUES ('s-stat01', 'episodic', 'anime', 'Show', 'english', 'planned', ?, ?)",
        (NOW, NOW),
    )
    yield c
    c.close()


def _season(conn, zid, n, status, *, kind="tvdb_season", parent=None, part=1, manual=0):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, part_number, source, status, kind,"
        " parent_id, status_set_manually, created_at, updated_at)"
        " VALUES (?, 's-stat01', ?, ?, 'manual', ?, ?, ?, ?, ?, ?)",
        (zid, n, part, status, kind, parent, manual, NOW, NOW),
    )


def _eps(conn, season, count, *, air=PAST, state="unwatched", start_abs=None):
    for e in range(1, count + 1):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, state,"
            " absolute_number, created_at, updated_at)"
            " VALUES (?, 's-stat01', ?, ?, 'regular', ?, ?, ?, ?, ?)",
            (f"e-{season}{e:05d}", season, e, air, state,
             None if start_abs is None else start_abs + e - 1, NOW, NOW),
        )


def _span(conn, zid, a, b):
    conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                 (zid, a, b))


def _status(conn, zid):
    return conn.execute("SELECT status FROM season WHERE id = ?", (zid,)).fetchone()[0]


def _show(conn):
    return conn.execute("SELECT status FROM show WHERE id = 's-stat01'").fetchone()[0]


def _watch(conn, season, episode):
    conn.execute(
        "UPDATE episode SET state = 'watched' WHERE season = ? AND episode = ?", (season, episode)
    )


def test_show_follows_its_last_non_skipped_season(conn):
    _season(conn, "z-s10000", 1, "completed")
    _season(conn, "z-s20000", 2, "planned")
    _season(conn, "z-s30000", 3, "skipped")
    assert status_rules.derive_show_status(conn, "s-stat01") == "planned"  # R2.13


def test_every_season_skipped_makes_the_show_skipped(conn):
    _season(conn, "z-s10000", 1, "skipped")
    assert status_rules.derive_show_status(conn, "s-stat01") == "skipped"


def test_planned_season_with_a_watched_episode_becomes_watching(conn):
    _season(conn, "z-s10000", 1, "planned")
    _eps(conn, 1, 3)
    _watch(conn, 1, 1)
    status_rules.after_episodes_changed(conn, "s-stat01")
    assert _status(conn, "z-s10000") == "watching"  # R2.14
    assert _show(conn) == "watching"


@pytest.mark.parametrize("status", ["paused", "dropped", "watching"])
def test_every_episode_watched_completes_the_level(conn, status):
    _season(conn, "z-s10000", 1, status)
    _eps(conn, 1, 2, state="watched")
    status_rules.after_episodes_changed(conn, "s-stat01")
    assert _status(conn, "z-s10000") == "completed"  # R2.15, paused/dropped included


def test_a_skipped_season_is_not_completed_by_watches(conn):
    _season(conn, "z-s10000", 1, "skipped")
    _eps(conn, 1, 1, state="watched")
    status_rules.after_episodes_changed(conn, "s-stat01")
    assert _status(conn, "z-s10000") == "skipped"


def test_setting_completed_asks_before_marking_unaired_episodes(conn):
    _season(conn, "z-s10000", 1, "watching")
    _eps(conn, 1, 1)
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, created_at,"
        " updated_at) VALUES ('e-futur1', 's-stat01', 1, 2, 'regular', ?, ?, ?)",
        (FUTURE, NOW, NOW),
    )
    with pytest.raises(status_rules.NeedsConfirmation, match="1 unaired"):
        status_rules.set_level_status(conn, "z-s10000", "completed", "web")
    fx = status_rules.set_level_status(conn, "z-s10000", "completed", "web", confirmed=True)
    states = {r[0] for r in conn.execute("SELECT state FROM episode")}
    assert states == {"watched"}  # R2.7: all of them, unaired included
    assert len(fx.watched) == 2
    assert conn.execute("SELECT COUNT(*) FROM watch_event").fetchone()[0] == 2


def test_new_season_after_a_stop_is_skipped_and_after_progress_planned(conn):
    _season(conn, "z-s10000", 1, "completed")
    assert status_rules.new_season_status(conn, "s-stat01", 2) == "planned"  # R2.16
    _season(conn, "z-s20000", 2, "dropped")
    assert status_rules.new_season_status(conn, "s-stat01", 3) == "skipped"


def test_earlier_season_found_late_is_skipped(conn):
    _season(conn, "z-s30000", 3, "watching")
    assert status_rules.new_season_status(conn, "s-stat01", 2) == "skipped"  # R2.19


def test_dropping_a_season_skips_later_auto_planned_ones(conn):
    _season(conn, "z-s10000", 1, "completed")
    _season(conn, "z-s20000", 2, "watching")
    _season(conn, "z-s30000", 3, "planned")  # auto-added
    status_rules.set_level_status(conn, "z-s20000", "dropped", "web")
    assert _status(conn, "z-s30000") == "skipped"  # R2.16 / Q-J3
    assert _show(conn) == "dropped"  # R2.13: last non-skipped season


def test_later_seasons_you_set_planned_are_skipped_only_after_a_warning(conn):
    _season(conn, "z-s10000", 1, "watching")
    _season(conn, "z-s20000", 2, "planned", manual=1)
    with pytest.raises(status_rules.NeedsConfirmation, match="you set planned"):
        status_rules.set_level_status(conn, "z-s10000", "paused", "web")
    assert _status(conn, "z-s10000") == "watching"
    status_rules.set_level_status(conn, "z-s10000", "paused", "web", confirmed=True)
    assert _status(conn, "z-s20000") == "skipped"  # Q-J4


def test_show_status_goes_on_the_last_non_skipped_season(conn):
    _season(conn, "z-s10000", 1, "completed")
    _season(conn, "z-s20000", 2, "watching")
    _season(conn, "z-s30000", 3, "skipped")
    status_rules.set_show_status(conn, "s-stat01", "paused", "web")
    # R2.13a
    assert (_status(conn, "z-s10000"), _status(conn, "z-s20000")) == ("completed", "paused")
    assert _show(conn) == "paused"


def test_tvdb_season_cascades_to_parts_but_a_completed_part_stays(conn):
    _season(conn, "z-s20000", 2, "watching")
    _season(conn, "z-p10000", 2, "completed", kind="part", parent="z-s20000")
    _season(conn, "z-p20000", 2, "planned", kind="part", parent="z-s20000", part=2)
    status_rules.set_level_status(conn, "z-s20000", "dropped", "web")
    # R2.18
    assert (_status(conn, "z-p10000"), _status(conn, "z-p20000")) == ("completed", "dropped")


def test_tvdb_season_status_comes_from_its_parts(conn):
    _season(conn, "z-s20000", 2, "planned")
    _season(conn, "z-p10000", 2, "planned", kind="part", parent="z-s20000")
    _season(conn, "z-p20000", 2, "planned", kind="part", parent="z-s20000", part=2)
    _span(conn, "z-s20000", 1, 4)
    _span(conn, "z-p10000", 1, 2)
    _span(conn, "z-p20000", 3, 4)
    _eps(conn, 2, 4, start_abs=1)
    _watch(conn, 2, 1)
    _watch(conn, 2, 2)
    status_rules.after_episodes_changed(conn, "s-stat01")
    # Part 1 completed, part 2 planned → the TVDB season is watching (R2.18).
    assert (_status(conn, "z-p10000"), _status(conn, "z-p20000")) == ("completed", "planned")
    assert _status(conn, "z-s20000") == "watching"
    assert _show(conn) == "watching"


def test_a_season_without_a_status_changes_nothing(conn):
    conn.execute("UPDATE show SET status = 'watching'")
    _season(conn, "z-s10000", 1, None)
    status_rules.recompute_show(conn, "s-stat01", "test")
    assert _show(conn) == "watching"
