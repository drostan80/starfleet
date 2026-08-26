"""PC.2 AniList-score historical import (`scripts/import_anilist_scores.py`).

Exercised against a real, migrated SQLite database (same reasoning as
test_availability.py — real FK/CHECK constraints, generated columns) so
the two passes are tested against the actual `season`/`show`/
`score_change` schema, not a hand-rolled stand-in. No real AniList call
happens here: the passes take an already-built `{anilist_id -> POINT_100}`
dict, and the one function that does fetch (`_anilist_scores_by_id`) is
tested with a monkeypatched `fetch_my_anime_list`.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_anilist_scores.py"
_spec = importlib.util.spec_from_file_location("import_anilist_scores", _SCRIPT)
importer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(importer)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "scores_test.db"
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


def _add_show(conn, show_id, score=None):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, score, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', 'Test', 'romaji', 'completed', 1, ?, 'x', 'x')",
        (show_id, score),
    )


def _add_tv_show(conn, show_id):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'tv', 'Test', 'english', 'completed', 1, 'x', 'x')",
        (show_id,),
    )


def _add_season(conn, season_id, show_id, season_number, anilist_id=None, score=None):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id, source, matched,"
        " score, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 1, ?, 'x', 'x')",
        (season_id, show_id, season_number, anilist_id, score),
    )


# --- scale conversion ---------------------------------------------------------


def test_to_lcars_score_snaps_to_quarter_points():
    assert importer._to_lcars_score(100) == 20.0
    assert importer._to_lcars_score(85) == 17.0
    assert importer._to_lcars_score(87) == 17.5  # 17.4 -> nearest quarter
    assert importer._to_lcars_score(50) == 10.0


# --- _anilist_scores_by_id drops 0/None ---------------------------------------


def test_anilist_scores_by_id_drops_unscored(monkeypatch):
    monkeypatch.setattr(
        anilist_client,
        "fetch_my_anime_list",
        lambda token: [
            {"anilist_id": 1, "score": 90},
            {"anilist_id": 2, "score": 0},  # AniList's "unscored"
            {"anilist_id": 3, "score": None},
        ],
    )
    assert importer._anilist_scores_by_id("tok") == {1: 90}


# --- season pass --------------------------------------------------------------


def test_season_pass_fills_only_null_scores(conn):
    _add_show(conn, "s-000001")
    _add_season(conn, "z-000001", "s-000001", 1, anilist_id=101)  # NULL -> fill
    _add_season(conn, "z-000002", "s-000001", 2, anilist_id=102, score=12.0)  # already scored
    _add_season(conn, "z-000003", "s-000001", 3, anilist_id=103)  # no anilist score
    conn.commit()

    result = importer.run_season_pass(conn, {101: 87, 102: 40})

    assert result["filled"] == 1
    assert result["skipped_already_scored"] == 1
    assert result["skipped_no_anilist_score"] == 1
    scores = {
        r["id"]: r["score"]
        for r in conn.execute("SELECT id, score FROM season").fetchall()
    }
    assert scores["z-000001"] == 17.5  # 87/5 -> 17.4 -> 17.5
    assert scores["z-000002"] == 12.0  # untouched
    assert scores["z-000003"] is None  # no anilist score, untouched


# --- show pass ----------------------------------------------------------------


def test_show_pass_fills_unambiguous_show_and_records_history(conn):
    _add_show(conn, "s-000001")
    _add_season(conn, "z-000001", "s-000001", 1, anilist_id=101)
    conn.commit()

    result = importer.run_show_pass(conn, {101: 90})

    assert result["filled"] == 1
    assert result["divergent"] == []
    row = conn.execute("SELECT score FROM show WHERE id = 's-000001'").fetchone()
    assert row["score"] == 18.0
    change = conn.execute(
        "SELECT previous_score, new_score, changed_by FROM score_change WHERE show_id = 's-000001'"
    ).fetchone()
    assert change["previous_score"] is None
    assert change["new_score"] == 18.0
    assert change["changed_by"] == "anilist_import"


def test_show_pass_agreeing_seasons_collapse_to_one_score(conn):
    _add_show(conn, "s-000001")
    _add_season(conn, "z-000001", "s-000001", 1, anilist_id=101)
    _add_season(conn, "z-000002", "s-000001", 2, anilist_id=102)
    conn.commit()

    result = importer.run_show_pass(conn, {101: 90, 102: 90})

    assert result["filled"] == 1
    assert result["divergent"] == []
    assert conn.execute("SELECT score FROM show WHERE id = 's-000001'").fetchone()["score"] == 18.0


def test_show_pass_divergent_seasons_leave_show_untouched(conn):
    _add_show(conn, "s-000001")
    _add_season(conn, "z-000001", "s-000001", 1, anilist_id=101)
    _add_season(conn, "z-000002", "s-000001", 2, anilist_id=102)
    conn.commit()

    result = importer.run_show_pass(conn, {101: 90, 102: 70})

    assert result["filled"] == 0
    assert len(result["divergent"]) == 1
    assert result["divergent"][0][0] == "s-000001"
    assert result["divergent"][0][1] == "Test"  # display title, for an actionable report
    assert result["divergent"][0][2] == [14.0, 18.0]
    assert conn.execute("SELECT score FROM show WHERE id = 's-000001'").fetchone()["score"] is None
    assert conn.execute("SELECT COUNT(*) c FROM score_change").fetchone()["c"] == 0


def test_show_pass_skips_already_scored_and_no_data(conn):
    _add_show(conn, "s-000001", score=15.0)  # already scored
    _add_season(conn, "z-000001", "s-000001", 1, anilist_id=101)
    _add_show(conn, "s-000002")  # no anilist score for its season
    _add_season(conn, "z-000002", "s-000002", 1, anilist_id=999)
    conn.commit()

    result = importer.run_show_pass(conn, {101: 90})

    assert result["filled"] == 0
    assert result["skipped_already_scored"] == 1
    assert result["skipped_no_data"] == 1


def test_show_pass_ignores_tv_shows(conn):
    _add_tv_show(conn, "s-000009")
    _add_season(conn, "z-000009", "s-000009", 1, anilist_id=101)
    conn.commit()

    result = importer.run_show_pass(conn, {101: 90})

    assert result["anime_shows"] == 0
    assert result["filled"] == 0
