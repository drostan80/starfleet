"""What no longer opens a review (10-05): repeated identical findings, network blips, a clamped
list progress, a show with no AniList entry, a changed numbering scheme."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import (
    anilist_client,
    config,
    external_writes,
    list_baseline,
    list_sync,
    metadata,
    pending_review,
)

NOW = "2026-10-05T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "noise.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " tracked, created_at, updated_at)"
        " VALUES ('s-nois01', 'episodic', 'anime', 'N', 'romaji', 'watching', 1, ?, ?)",
        (NOW, NOW))
    c.commit()
    config.set_current(config.Config())
    yield c
    c.close()


def _count(conn, field=None):
    sql = "SELECT COUNT(*) FROM pending_review WHERE resolved_at IS NULL"
    return conn.execute(sql + (" AND field = ?" if field else ""),
                        (field,) if field else ()).fetchone()[0]


def test_the_same_finding_again_does_not_grow_the_chain(conn):
    for _ in range(5):
        pending_review.open_or_extend(conn, "show", "s-nois01", "anilist_id", "x", None, "same")
    pending_review.open_or_extend(conn, "show", "s-nois01", "anilist_id", "x", None, "different")
    pending_review.open_or_extend(conn, "show", "s-nois01", "anilist_id", "x", None, "same")
    chain = conn.execute("SELECT proposed_value_chain FROM pending_review").fetchone()[0]
    assert chain == '["same", "different", "same"]'  # real changes still accumulate


@pytest.mark.parametrize("message,transient", [
    ("Could not connect to AniList", True),
    ("AniList returned an error: HTTP 429", True),
    ("Could not connect to MyAnimeList", True),
    ("Timed out talking to LCARS", True),
    ("AniList returned an error: invalid media id", False),
    ("season 1 has 24 episodes in LCARS but AniList only covers 12", False),
])
def test_which_errors_are_blips(message, transient):
    assert pending_review.is_transient_error(message) is transient


def test_a_failing_metadata_step_opens_a_review_only_for_a_real_error(conn):
    show = dict(conn.execute("SELECT * FROM show WHERE id = 's-nois01'").fetchone())

    def blip(_conn, _show):
        raise anilist_client.AniListError("Could not connect to AniList")

    def broken(_conn, _show):
        raise ValueError("a genuine bug")

    metadata._guarded(conn, show, "anilist", blip)
    assert _count(conn, "metadata_fetch") == 0
    metadata._guarded(conn, show, "anilist", broken)
    assert _count(conn, "metadata_fetch") == 1


def test_a_service_clamping_progress_to_the_entrys_own_count_is_not_a_review(conn, monkeypatch):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, episode_total, mal_id, source,"
        " created_at, updated_at) VALUES ('z-nois01', 's-nois01', 1, 'completed', 5, 200,"
        " 'manual', ?, ?)", (NOW, NOW))
    conn.commit()
    monkeypatch.setattr(external_writes, "capturing", lambda: False)
    monkeypatch.setattr(list_baseline, "get",
                        lambda *_a: {"status": "completed", "progress": 5})
    season = conn.execute("SELECT * FROM season WHERE id = 'z-nois01'").fetchone()
    list_sync._read_back(conn, config.get_current(), season, "mal", 200, 9)  # LCARS 9, MAL 5/5
    assert _count(conn, "list_readback_differs") == 0
    monkeypatch.setattr(list_baseline, "get",
                        lambda *_a: {"status": "completed", "progress": 3})
    list_sync._read_back(conn, config.get_current(), season, "mal", 200, 9)  # 3 is not the clamp
    assert _count(conn, "list_readback_differs") == 1
