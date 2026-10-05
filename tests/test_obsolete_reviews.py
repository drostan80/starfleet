"""A review whose finding no longer holds closes itself (user, 10-05: reviews that cannot be
actioned): Apothecary Diaries, R.O.D, Kanojo no Tomodachi."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import fribb, identity_mismatch, pending_review

ROOT = Path(__file__).resolve().parent.parent
T = "2026-10-05T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "rv.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " tracked, created_at, updated_at)"
        " VALUES ('s-rv0001', 'episodic', 'anime', 'Show', 'romaji', 'watching', 1, ?, ?)", (T, T))
    c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
              " VALUES ('s-rv0001', 'tvdb', '82319', 'x', ?)", (T,))
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, status, source,"
        " created_at, updated_at) VALUES ('z-rv0001', 's-rv0001', 1, 'tvdb_season', 209,"
        " 'planned', 'manual', ?, ?)", (T, T))
    c.commit()
    return c


def open_count(conn):
    return conn.execute("SELECT COUNT(*) FROM pending_review WHERE resolved_at IS NULL"
                        ).fetchone()[0]


def test_close_obsolete_closes_only_the_matching_open_review(conn):
    pending_review.open_or_extend(conn, "season", "z-rv0001", "anilist_id", "fribb", None, "1")
    pending_review.open_or_extend(conn, "season", "z-rv0001", "mal_id", "fribb", None, "2")
    n = pending_review.close_obsolete(conn, "season", "z-rv0001", "anilist_id", "gone",
                                      source="fribb")
    assert n == 1 and open_count(conn) == 1
    assert pending_review.close_obsolete(conn, "season", "z-rv0001", "anilist_id", "again") == 0


def test_the_identity_review_closes_when_the_season_now_agrees_with_fribb(conn, monkeypatch):
    # R.O.D: S1 held 208 while Fribb says 209 -> a review; the swap put 209 on S1
    pending_review.open_or_extend(conn, "season", "z-rv0001", "anilist_id",
                                  "fribb_identity_mismatch", 208, 209)
    conn.commit()
    monkeypatch.setattr(fribb, "load_dataset", lambda *a, **k: [
        {"type": "TV", "tvdb_id": 82319, "anilist_id": 209, "mal_id": 209, "season": {"tvdb": 1}}])
    identity_mismatch.check_anilist_id_mismatch(conn)
    assert open_count(conn) == 0
    note = conn.execute("SELECT resolution_note FROM pending_review").fetchone()[0]
    assert "agrees with Fribb" in note
