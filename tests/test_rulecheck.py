"""rulecheck (PLAN-CODE phase 1): read-only check of a database against RULEBOOK.md."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import rulecheck

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "rulecheck.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    return path


def _write(path, sql, params=()):
    c = sqlite3.connect(path)
    c.execute(sql, params)
    c.commit()
    c.close()


def _show(path, sid, title, status="completed", tracked=1, tvdb="100"):
    _write(
        path,
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'english', ?, ?, ?, ?)",
        (sid, title, status, tracked, NOW, NOW),
    )
    if tvdb:
        _write(
            path,
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tvdb', ?, '', ?)",
            (sid, tvdb, NOW),
        )


def _season(path, zid, sid, n, status, start, end):
    _write(
        path,
        "INSERT INTO season (id, show_id, season_number, source, status, abs_start,"
        " abs_end, created_at, updated_at) VALUES (?, ?, ?, 'manual', ?, ?, ?, ?, ?)",
        (zid, sid, n, status, start, end, NOW, NOW),
    )


def _episode(path, eid, sid, zid, season, ep, abs_n, state="watched"):
    _write(
        path,
        "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number,"
        " air_date_utc, state, season_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, '2020-01-01T00:00:00Z', ?, ?, ?, ?)",
        (eid, sid, season, ep, abs_n, state, zid, NOW, NOW),
    )


def _by_rule(path):
    return {f.rule: f for f in rulecheck.run(rulecheck.open_readonly(str(path)))}


def test_clean_database_has_no_violation(db_path):
    _show(db_path, "s-aaaaaa", "Clean Show")
    _season(db_path, "z-aaaaaa", "s-aaaaaa", 1, "completed", 1, 2)
    _episode(db_path, "e-aaaaa1", "s-aaaaaa", "z-aaaaaa", 1, 1, 1)
    _episode(db_path, "e-aaaaa2", "s-aaaaaa", "z-aaaaaa", 1, 2, 2)
    found = rulecheck.run(rulecheck.open_readonly(str(db_path)))
    assert [f.rule for f in found if f.kind == "violation" and f.count] == []


def test_planted_violations_are_found(db_path):
    _show(db_path, "s-bbbbbb", "Broken Show", status="watching")
    _show(db_path, "s-cccccc", "Duplicate Of It", tvdb="100")
    _show(db_path, "s-dddddd", "Stub", status="planned", tracked=0, tvdb=None)  # R3.5
    _season(db_path, "z-bbbbb1", "s-bbbbbb", 1, "completed", 1, 2)
    _season(db_path, "z-bbbbb2", "s-bbbbbb", 2, "planned", 3, 3)
    _episode(db_path, "e-bbbbb1", "s-bbbbbb", "z-bbbbb1", 1, 1, 1)
    _episode(
        db_path, "e-bbbbb2", "s-bbbbbb", "z-bbbbb1", 1, 2, None, state="unwatched"
    )  # R1.0, R2.7
    _episode(db_path, "e-bbbbb3", "s-bbbbbb", None, 0, 1, 1.1)  # R1.8: season 0, no season link
    found = _by_rule(db_path)
    assert found["R1.0"].count == 1
    assert found["R1.8"].count == 1
    assert found["R2.7"].count == 1
    assert found["R2.13"].count == 1  # show watching, last season planned
    assert found["R3.5"].count == 1
    assert found["R1.14"].count == 1  # both tracked shows carry tvdb 100


def test_database_is_opened_read_only(db_path):
    conn = rulecheck.open_readonly(str(db_path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM show")


def test_cli_exit_code(db_path, capsys):
    assert rulecheck.main([str(db_path)]) == 0
    _show(db_path, "s-eeeeee", "Stub", status="planned", tracked=0, tvdb=None)
    assert rulecheck.main([str(db_path)]) == 1
    assert "R3.5" in capsys.readouterr().out
