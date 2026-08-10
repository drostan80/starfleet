"""untracked_sweep.py — SCOPE.md §5.2's "Resolved 2026-08-10 (B.11
reconnaissance)" note, BUILD_PLAN.md B.11e. `show_backfill.preview_backfill`
is monkeypatched directly throughout — its own classification/dedup
logic is already covered by test_show_backfill.py; these tests are
about this module's own reconciliation against untracked_show_finding
(insert/refresh/prune), not re-deriving what "untracked" means. Same
real-migrated-SQLite-DB fixture test_show_backfill.py already
established.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import show_backfill, untracked_sweep


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "untracked_sweep_test.db"
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


_ITEM_A = {
    "service": "sonarr",
    "title": "Show A",
    "external_id": 111,
    "path": "/data/anime/Show A",
    "tracking_space": "anime",
    "media_shape": "episodic",
}
_ITEM_B = {
    "service": "anilist",
    "title": "Show B",
    "external_id": 222,
    "path": None,
    "tracking_space": "anime",
    "media_shape": "movie",
}


def _findings(conn):
    return conn.execute(
        "SELECT * FROM untracked_show_finding ORDER BY service, external_id"
    ).fetchall()


def test_sweep_inserts_new_findings(conn, monkeypatch):
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [_ITEM_A, _ITEM_B])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 2, "new_findings": 2, "resolved_findings": 0}

    rows = _findings(conn)
    assert len(rows) == 2
    assert rows[1]["service"] == "sonarr"
    assert rows[1]["external_id"] == "111"
    assert rows[1]["title"] == "Show A"
    assert rows[1]["path"] == "/data/anime/Show A"
    assert rows[1]["tracking_space"] == "anime"
    assert rows[1]["media_shape"] == "episodic"
    assert rows[1]["first_seen_at"] == rows[1]["last_seen_at"]
    assert rows[1]["id"].startswith("u-")

    assert rows[0]["service"] == "anilist"
    assert rows[0]["path"] is None  # never had one — AniList-sourced


def test_sweep_never_creates_a_show(conn, monkeypatch):
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [_ITEM_A])
    untracked_sweep.sweep_untracked_shows(conn)
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0


def test_sweep_is_a_clean_no_op_on_an_unchanged_rerun(conn, monkeypatch):
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [_ITEM_A])
    untracked_sweep.sweep_untracked_shows(conn)
    first_seen = _findings(conn)[0]["first_seen_at"]

    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 0}
    rows = _findings(conn)
    assert len(rows) == 1  # not duplicated
    assert rows[0]["first_seen_at"] == first_seen  # preserved, not reset


def test_sweep_refreshes_a_changed_field_on_a_still_current_finding(conn, monkeypatch):
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [_ITEM_A])
    untracked_sweep.sweep_untracked_shows(conn)

    renamed = {**_ITEM_A, "title": "Show A (renamed in Sonarr)"}
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [renamed])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 0}

    rows = _findings(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Show A (renamed in Sonarr)"


def test_sweep_prunes_a_finding_no_longer_present(conn, monkeypatch):
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [_ITEM_A, _ITEM_B])
    untracked_sweep.sweep_untracked_shows(conn)

    # Show A got tracked some other way (or genuinely disappeared) — only
    # B is still untracked next sweep.
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [_ITEM_B])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 1}

    rows = _findings(conn)
    assert len(rows) == 1
    assert rows[0]["service"] == "anilist"


def test_sweep_is_empty_with_nothing_untracked(conn, monkeypatch):
    monkeypatch.setattr(show_backfill, "preview_backfill", lambda c: [])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 0, "new_findings": 0, "resolved_findings": 0}
    assert _findings(conn) == []
