"""Phase 2 layout (PLAN-CODE 2.1–2.3): levels, spans, decimal season numbers.

The old columns stay during the transition; a trigger keeps the decimal
season number filled from `season_number`. Spans are written by Memory
Alpha's numbering engine (phase 3.2)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "levels.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")  # as lcars.db.connect does
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, created_at, updated_at)"
        " VALUES ('s-aaaaaa', 'episodic', 'anime', 'Show', 'english', 'watching', ?, ?)",
        (NOW, NOW),
    )
    yield c
    c.close()


def _season(conn, zid, n=1, start=None, end=None, **extra):
    cols = {"id": zid, "show_id": "s-aaaaaa", "season_number": n, "source": "manual",
            "abs_start": start, "abs_end": end, "created_at": NOW, "updated_at": NOW, **extra}
    conn.execute(
        f"INSERT INTO season ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )


def _spans(conn, zid):
    return conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = ? ORDER BY abs_from", (zid,)
    ).fetchall()


def test_old_writers_fill_the_decimal_number(conn):
    _season(conn, "z-aaaaa1", 2, 13, 24)
    kind, number = conn.execute(
        "SELECT kind, decimal_season_number FROM season WHERE id = 'z-aaaaa1'"
    ).fetchone()
    assert (kind, number) == ("tvdb_season", 2.0)
    conn.execute("UPDATE season SET season_number = 3 WHERE id = 'z-aaaaa1'")
    assert conn.execute(
        "SELECT decimal_season_number FROM season WHERE id = 'z-aaaaa1'"
    ).fetchone() == (3.0,)
    # Spans come from Memory Alpha's numbering engine only (phase 3.2).
    assert _spans(conn, "z-aaaaa1") == []


def test_season_without_range_has_no_span(conn):
    _season(conn, "z-aaaaa1")
    assert _spans(conn, "z-aaaaa1") == []


def test_deleting_a_season_deletes_its_spans(conn):
    _season(conn, "z-aaaaa1", 1, 1, 12)
    conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES ('z-aaaaa1', 1, 12)")
    conn.execute("DELETE FROM season WHERE id = 'z-aaaaa1'")
    assert conn.execute("SELECT COUNT(*) FROM season_span").fetchone() == (0,)


def test_side_piece_keeps_its_own_decimal_number(conn):
    # R1.9a: one side piece between S2 and S3 → S2.5, no TVDB season number.
    _season(conn, "z-aaaaa1", None, 25, 27, kind="special", decimal_season_number=2.5)
    assert conn.execute(
        "SELECT season_number, decimal_season_number FROM season WHERE id = 'z-aaaaa1'"
    ).fetchone() == (None, 2.5)


def test_part_needs_a_parent_and_a_tvdb_season_has_none(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _season(conn, "z-aaaaa1", 2, 14, 26, kind="part")
    _season(conn, "z-aaaaa1", 2, 14, 40)
    with pytest.raises(sqlite3.IntegrityError):
        _season(conn, "z-aaaaa2", 3, 41, 52, parent_id="z-aaaaa1")
    _season(conn, "z-aaaaa2", 2, 14, 26, part_number=2, kind="part", parent_id="z-aaaaa1")


def test_individual_season_has_no_show(conn):
    # R3.6c: a new planned season TVDB doesn't have yet.
    _season(conn, "z-aaaaa1", None, show_id=None, status="planned", kind="individual_season")
    assert conn.execute("SELECT show_id FROM season WHERE id = 'z-aaaaa1'").fetchone() == (None,)
    # R3.6d: a TVDB season always has its show and number; an individual one never a show.
    with pytest.raises(sqlite3.IntegrityError):
        _season(conn, "z-aaaaa2", None, show_id=None, status="planned")
    with pytest.raises(sqlite3.IntegrityError):
        _season(conn, "z-aaaaa3", 4, kind="individual_season")


def test_status_before_pause_is_gone(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(show)")]
    assert "status_before_pause" not in cols
