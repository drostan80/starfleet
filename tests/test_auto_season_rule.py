"""Seasons LCARS creates on its own (user rule, 2026-09-25): status follows
the previous season, future seasons are planned, and none of them is ever
written to AniList/MAL (`season.list_sync = 0`)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, mal_client, resolvers, season_ranges


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "auto_season.db"
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


def _show(conn, show_id, status):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', 'T', 'romaji', ?, 1, 'x', 'x')",
        (show_id, status),
    )


def _season(conn, season_id, show_id, n, status, anilist_id=None, mal_id=None, list_sync=1):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id, mal_id,"
        " source, created_at, updated_at, list_sync)"
        " VALUES (?, ?, ?, ?, ?, ?, 'fribb', 'x', 'x', ?)",
        (season_id, show_id, n, status, anilist_id, mal_id, list_sync),
    )


@pytest.mark.parametrize("previous,expected", [
    ("completed", "paused"),
    ("watching", "paused"),
    ("paused", "paused"),
    ("dropped", "dropped"),
    ("planned", "planned"),
])
def test_finished_season_follows_the_previous_season(conn, previous, expected):
    _show(conn, "s-rule01", "watching")
    _season(conn, "z-rule01", "s-rule01", 1, previous, anilist_id=1)
    conn.commit()
    status, list_sync = season_ranges.auto_season_fields(
        conn, "s-rule01", 2, anilist_id=2, finished=True
    )
    assert (status, list_sync) == (expected, 0)


def test_future_season_is_planned_even_after_a_dropped_one(conn):
    _show(conn, "s-rule02", "dropped")
    _season(conn, "z-rule02", "s-rule02", 1, "dropped", anilist_id=1)
    conn.commit()
    status, list_sync = season_ranges.auto_season_fields(
        conn, "s-rule02", 2, anilist_id=2, finished=False
    )
    assert (status, list_sync) == ("planned", 0)


def test_a_new_shows_first_season_is_the_users_and_stays_synced(conn):
    _show(conn, "s-rule03", "planned")
    conn.commit()
    status, list_sync = season_ranges.auto_season_fields(conn, "s-rule03", 1, anilist_id=10)
    assert list_sync == 1


def test_release_status_comes_from_anilist_when_there_are_no_episodes(conn, monkeypatch):
    monkeypatch.setattr(
        anilist_client, "fetch_media_statuses", lambda ids, client=None: {2: "FINISHED"}
    )
    _show(conn, "s-rule04", "completed")
    _season(conn, "z-rule04", "s-rule04", 1, "completed", anilist_id=1)
    conn.commit()
    assert season_ranges.auto_season_fields(conn, "s-rule04", 2, anilist_id=2) == ("paused", 0)


def test_show_status_change_never_pushes_a_list_sync_0_season(conn, monkeypatch):
    # Real incident class: every show status change pushed every season,
    # which would have added 86 auto-created "dropped" seasons to the lists.
    config.set_current(config.Config(anilist_access_token="a", mal_access_token="m"))
    anilist_calls, mal_calls = [], []
    monkeypatch.setattr(
        anilist_client, "save_media_list_entry",
        lambda token, media_id, **kw: anilist_calls.append(media_id),
    )
    monkeypatch.setattr(
        mal_client, "update_my_list_status",
        lambda token, mal_id, **kw: mal_calls.append(mal_id),
    )
    _show(conn, "s-rule05", "watching")
    _season(conn, "z-rule05", "s-rule05", 1, "completed", anilist_id=100, mal_id=200)
    _season(conn, "z-rule06", "s-rule05", 2, "dropped", anilist_id=101, mal_id=201, list_sync=0)
    conn.commit()

    resolvers._push_show_status(conn, "s-rule05", "completed")
    resolvers._push_mal_show_status(conn, "s-rule05", "completed")
    season = dict(conn.execute("SELECT * FROM season WHERE id = 'z-rule06'").fetchone())
    resolvers._push_season_progress(conn, season)
    resolvers._push_mal_season_status(conn, season, "dropped")

    assert anilist_calls == [100]
    assert mal_calls == [200]


def test_acting_on_a_season_turns_list_sync_on(conn):
    _show(conn, "s-rule07", "watching")
    _season(conn, "z-rule07", "s-rule07", 2, "paused", anilist_id=5, list_sync=0)
    conn.commit()
    season = dict(conn.execute("SELECT * FROM season WHERE id = 'z-rule07'").fetchone())
    resolvers._enable_list_sync(conn, season)
    assert conn.execute("SELECT list_sync FROM season WHERE id = 'z-rule07'").fetchone()[0] == 1


def test_completing_a_season_sets_mal_progress_to_the_mal_entrys_own_count(conn, monkeypatch):
    # 09-23: completed seasons showed 0/N on MAL. The count comes from the
    # MAL entry (13 here), not the LCARS season's episodes (2 here).
    config.set_current(config.Config(mal_access_token="m", mal_client_id="cid"))
    sent = []
    monkeypatch.setattr(
        mal_client, "update_my_list_status",
        lambda token, mal_id, **kw: sent.append(kw) or {},
    )
    monkeypatch.setattr(
        mal_client, "fetch_anime_details",
        lambda mal_id, client_id, client=None: {"num_episodes": 13},
    )
    _show(conn, "s-rule08", "watching")
    _season(conn, "z-rule08", "s-rule08", 1, "completed", mal_id=300)
    conn.commit()
    season = dict(conn.execute("SELECT * FROM season WHERE id = 'z-rule08'").fetchone())

    resolvers._push_mal_season_status(conn, season, "completed")

    assert sent == [{"status": "completed", "num_watched_episodes": 13}]
