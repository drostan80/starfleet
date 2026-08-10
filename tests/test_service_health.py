"""Per-integration service-health tracking — SCOPE.md §6.7, BUILD_PLAN.md
B.6. Exercised against a real, migrated SQLite database (same reasoning
test_availability.py/test_local_audit.py already use).
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import service_health


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "service_health_test.db"
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


def test_get_all_returns_unknown_placeholders_with_no_rows_at_all(conn):
    conn.commit()
    result = service_health.get_all(conn)
    assert [r["service"] for r in result] == ["sonarr", "radarr", "anilist", "animeschedule"]
    assert all(r["status"] == "unknown" for r in result)
    assert all(r["last_checked_at"] is None for r in result)
    assert all(r["last_success_at"] is None for r in result)
    assert all(r["last_error_message"] is None for r in result)


def test_record_success_sets_ok_and_both_timestamps(conn):
    service_health.record_success(conn, "sonarr")
    conn.commit()
    row = next(r for r in service_health.get_all(conn) if r["service"] == "sonarr")
    assert row["status"] == "ok"
    assert row["last_checked_at"] is not None
    assert row["last_success_at"] == row["last_checked_at"]
    assert row["last_error_message"] is None
    # The other three stay unknown — this write is per-service, not global.
    others = [r for r in service_health.get_all(conn) if r["service"] != "sonarr"]
    assert all(r["status"] == "unknown" for r in others)


def test_record_failure_sets_unreachable_with_no_success_timestamp(conn):
    service_health.record_failure(conn, "radarr", "Could not connect to Radarr")
    conn.commit()
    row = next(r for r in service_health.get_all(conn) if r["service"] == "radarr")
    assert row["status"] == "unreachable"
    assert row["last_checked_at"] is not None
    assert row["last_success_at"] is None
    assert row["last_error_message"] == "Could not connect to Radarr"


def test_a_failure_after_a_success_keeps_the_earlier_last_success_at(conn):
    service_health.record_success(conn, "anilist")
    conn.commit()
    row = next(r for r in service_health.get_all(conn) if r["service"] == "anilist")
    first_success_at = row["last_success_at"]

    service_health.record_failure(conn, "anilist", "Timed out talking to AniList")
    conn.commit()
    row = next(r for r in service_health.get_all(conn) if r["service"] == "anilist")
    assert row["status"] == "unreachable"
    assert row["last_success_at"] == first_success_at  # untouched, not blanked
    assert row["last_error_message"] == "Timed out talking to AniList"


def test_a_success_after_a_failure_clears_the_error_message(conn):
    service_health.record_failure(conn, "animeschedule", "boom")
    conn.commit()
    service_health.record_success(conn, "animeschedule")
    conn.commit()
    row = next(r for r in service_health.get_all(conn) if r["service"] == "animeschedule")
    assert row["status"] == "ok"
    assert row["last_error_message"] is None


def test_an_untracked_service_is_silently_a_no_op(conn):
    service_health.record_success(conn, "tmdb")
    service_health.record_failure(conn, "tmdb", "irrelevant")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM service_health").fetchone()["n"] == 0
