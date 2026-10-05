"""animeschedule.net air-date reconciliation — SCOPE.md §5.2/§6.7,
BUILD_PLAN.md B.5. Exercised against a real, migrated SQLite database
(same reasoning test_availability.py/test_local_audit.py already use)
with a fake animeschedule_client (no real network call).
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import animeschedule, animeschedule_client, service_health


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "animeschedule_test.db"
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


def _add_show(conn, show_id, title_romaji="Test Show", status="watching", tracking_space="anime"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, ?, 'romaji', ?, 1, 'x', 'x')",
        (show_id, tracking_space, title_romaji, status),
    )
    conn.commit()


def _add_episode(
    conn, episode_id, show_id, season=1, episode=1, air_date_utc=None, air_date_source=None
):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc,"
        " air_date_source, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, ?, 'unwatched', 'x', 'x')",
        (episode_id, show_id, season, episode, air_date_utc, air_date_source),
    )
    conn.commit()


def _pending_reviews(conn, entity_id, field):
    return conn.execute(
        "SELECT * FROM pending_review WHERE entity_id = ? AND field = ?", (entity_id, field)
    ).fetchall()


def _item(title, episode, air_date_utc, guid=None):
    return {
        "guid": guid or f"{title} Episode {episode}",
        "title": title,
        "episode": episode,
        "air_date_utc": air_date_utc,
    }


def test_a_clear_match_updates_the_matching_episode(conn, monkeypatch):
    _add_show(conn, "s-aaaaaa", title_romaji="Digimon BeatBreak")
    _add_episode(
        conn,
        "e-aaaaaa",
        "s-aaaaaa",
        season=1,
        episode=42,
        air_date_utc="2099-01-01T00:00:00Z",
        air_date_source="sonarr",
    )
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Digimon BeatBreak", 42, "2026-08-09T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 1, "flagged": 0}
    row = conn.execute(
        "SELECT air_date_utc, air_date_source FROM episode WHERE id = ?", ("e-aaaaaa",)
    ).fetchone()
    assert row["air_date_utc"] == "2026-08-09T00:00:00Z"
    assert row["air_date_source"] == "animeschedule"
    # The applied change is history (air_date_change), not a review (10-05).
    assert _pending_reviews(conn, "e-aaaaaa", "air_date_utc") == []
    change = conn.execute(
        "SELECT previous_air_date_utc, new_air_date_utc, previous_source, new_source, changed_by"
        " FROM air_date_change WHERE episode_id = 'e-aaaaaa'"
    ).fetchone()
    assert tuple(change) == ("2099-01-01T00:00:00Z", "2026-08-09T00:00:00Z", "sonarr",
                             "animeschedule", "system")
    # §6.7, B.6 — a successful feed fetch records service_health, whether or
    # not anything ended up matching a tracked show.
    health = next(r for r in service_health.get_all(conn) if r["service"] == "animeschedule")
    assert health["status"] == "ok"


def test_animeschedule_skips_manual_date(conn, monkeypatch):
    """Revised 2026-09-08: animeschedule no longer overrides manual —
    airdate_priority.py's chain (manual > syoboi > animeschedule) means
    manual-sourced dates are protected from all automatic sources."""
    _add_show(conn, "s-bbbbbb", title_romaji="Some Show")
    _add_episode(
        conn,
        "e-bbbbbb",
        "s-bbbbbb",
        season=1,
        episode=6,
        air_date_utc="2030-06-01T00:00:00Z",
        air_date_source="manual",
    )
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Some Show", 6, "2026-08-09T09:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}
    row = conn.execute(
        "SELECT air_date_utc, air_date_source FROM episode WHERE id = ?", ("e-bbbbbb",)
    ).fetchone()
    # Manual date preserved — not overwritten
    assert row["air_date_utc"] == "2030-06-01T00:00:00Z"
    assert row["air_date_source"] == "manual"


def test_is_a_no_op_when_the_value_is_already_correct(conn, monkeypatch):
    _add_show(conn, "s-cccccc", title_romaji="Some Show")
    _add_episode(
        conn,
        "e-cccccc",
        "s-cccccc",
        season=1,
        episode=6,
        air_date_utc="2099-01-01T00:00:00Z",
        air_date_source="animeschedule",
    )
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Some Show", 6, "2099-01-01T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}
    assert _pending_reviews(conn, "e-cccccc", "air_date_utc") == []


def test_a_feed_item_not_matching_any_tracked_show_is_ignored(conn, monkeypatch):
    _add_show(conn, "s-dddddd", title_romaji="Tracked Show")
    _add_episode(conn, "e-dddddd", "s-dddddd", season=1, episode=1, air_date_utc=None)
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Completely Unrelated Global Anime", 1, "2026-08-09T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}


def test_a_non_watching_show_is_never_a_match_candidate(conn, monkeypatch):
    _add_show(conn, "s-eeeeee", title_romaji="Digimon BeatBreak", status="completed")
    _add_episode(
        conn,
        "e-eeeeee",
        "s-eeeeee",
        season=1,
        episode=42,
        air_date_utc="2020-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Digimon BeatBreak", 42, "2026-08-09T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}


def test_a_tv_tracking_space_show_is_never_a_match_candidate(conn, monkeypatch):
    _add_show(conn, "s-ffffff", title_romaji="Digimon BeatBreak", tracking_space="tv")
    _add_episode(
        conn,
        "e-ffffff",
        "s-ffffff",
        season=1,
        episode=42,
        air_date_utc=None,
    )
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Digimon BeatBreak", 42, "2026-08-09T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}


def test_a_fully_released_show_with_no_airing_episode_is_never_a_candidate(conn, monkeypatch):
    _add_show(conn, "s-gggggg", title_romaji="Digimon BeatBreak")
    _add_episode(
        conn,
        "e-gggggg",
        "s-gggggg",
        season=1,
        episode=42,
        air_date_utc="2020-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Digimon BeatBreak", 42, "2026-08-09T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}


def test_no_matching_episode_row_writes_nothing_and_opens_no_review(conn, monkeypatch):
    _add_show(conn, "s-hhhhhh", title_romaji="Digimon BeatBreak")
    # Only episode 1 exists (still airing) — the feed reports episode 42.
    _add_episode(conn, "e-hhhhhh", "s-hhhhhh", season=1, episode=1, air_date_utc=None)
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Digimon BeatBreak", 42, "2026-08-09T00:00:00Z")],
    )
    for _ in range(2):  # re-seen on the next hourly sweep
        result = animeschedule.poll_anime_schedule(conn)
        assert result == {"episodes_updated": 0, "flagged": 0}
    # An unclear match is logged, no longer a review (10-05).
    assert _pending_reviews(conn, "s-hhhhhh", "animeschedule_episode_match") == []
    assert conn.execute("SELECT air_date_utc FROM episode WHERE id = 'e-hhhhhh'"
                        ).fetchone()[0] is None


def test_multiple_currently_airing_seasons_sharing_an_episode_number_open_no_review(
    conn, monkeypatch
):
    _add_show(conn, "s-iiiiii", title_romaji="Digimon BeatBreak")
    _add_episode(conn, "e-iiiiii", "s-iiiiii", season=1, episode=6, air_date_utc=None)
    _add_episode(conn, "e-jjjjjj", "s-iiiiii", season=2, episode=6, air_date_utc=None)
    monkeypatch.setattr(
        animeschedule_client,
        "fetch_raw_feed",
        lambda: [_item("Digimon BeatBreak", 6, "2026-08-09T00:00:00Z")],
    )
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}
    assert _pending_reviews(conn, "s-iiiiii", "animeschedule_episode_match") == []
    assert conn.execute("SELECT COUNT(*) FROM episode WHERE air_date_utc IS NOT NULL"
                        ).fetchone()[0] == 0  # ambiguous: nothing guessed


def test_a_show_with_no_airing_season_is_left_alone_and_opens_no_review(conn):
    """Defensive branch (unreachable through poll_anime_schedule's own candidate filter)."""
    _add_show(conn, "s-nnnnnn", title_romaji="Digimon BeatBreak")
    outcome = animeschedule._apply_or_flag(
        conn, "s-nnnnnn", _item("Digimon BeatBreak", 42, "2026-08-09T00:00:00Z")
    )
    assert outcome == "unchanged"
    assert _pending_reviews(conn, "s-nnnnnn", "animeschedule_episode_match") == []


def test_a_feed_fetch_failure_is_a_clean_zero_result_not_a_raise(conn, monkeypatch):
    _add_show(conn, "s-llllll", title_romaji="Digimon BeatBreak")
    _add_episode(conn, "e-llllll", "s-llllll", season=1, episode=1, air_date_utc=None)

    def _boom():
        raise animeschedule_client.AnimeScheduleError("boom")

    monkeypatch.setattr(animeschedule_client, "fetch_raw_feed", _boom)
    assert animeschedule.poll_anime_schedule(conn) == {"episodes_updated": 0, "flagged": 0}
    health = next(r for r in service_health.get_all(conn) if r["service"] == "animeschedule")
    assert health["status"] == "unreachable"
    assert "boom" in health["last_error_message"]


def test_no_candidate_shows_at_all_skips_the_feed_fetch_entirely(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(animeschedule_client, "fetch_raw_feed", lambda: calls.append(1) or [])
    result = animeschedule.poll_anime_schedule(conn)
    assert result == {"episodes_updated": 0, "flagged": 0}
    assert calls == []
    # No attempt was made at all — status stays the "never contacted" default,
    # not a false "ok".
    health = next(r for r in service_health.get_all(conn) if r["service"] == "animeschedule")
    assert health["status"] == "unknown"
