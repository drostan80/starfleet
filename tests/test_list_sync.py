"""LCARS → AniList/MAL per level (PLAN-CODE phase 7) — RULEBOOK R1.23, R2.10, R4.5, R4.6."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, list_sync, mal_client

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "l.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " created_at, updated_at) VALUES ('s-list01', 'episodic', 'anime', 'L', 'romaji',"
        " 'watching', ?, ?)", (NOW, NOW))
    # TVDB S1 = two cours: part 1 (abs 1-2, AniList 101), part 2 (abs 3-4, AniList 102).
    c.execute("INSERT INTO season (id, show_id, season_number, source, status, created_at,"
              " updated_at) VALUES ('z-list00', 's-list01', 1, 'manual', 'watching', ?, ?)",
              (NOW, NOW))
    for zid, part, al, a, b in (("z-list01", 1, 101, 1, 2), ("z-list02", 2, 102, 3, 4)):
        c.execute(
            "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
            " anilist_id, source, status, created_at, updated_at)"
            " VALUES (?, 's-list01', 1, ?, 'part', 'z-list00', ?, 'auto', 'watching', ?, ?)",
            (zid, part, al, NOW, NOW))
        c.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                  (zid, a, b))
    for e in range(1, 5):
        c.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number, state,"
            " air_date_utc, created_at, updated_at) VALUES (?, 's-list01', 1, ?, 'regular', ?,"
            " ?, '2020-01-01T00:00:00Z', ?, ?)",
            (f"e-list0{e}", e, e, "watched" if e <= 3 else "unwatched", NOW, NOW))
    config.set_current(config.Config(anilist_access_token="a", mal_access_token="m"))
    yield c
    config.set_current(config.Config())
    c.close()


def _season(conn, zid):
    return conn.execute("SELECT * FROM season WHERE id = ?", (zid,)).fetchone()


def test_a_cours_progress_counts_only_its_own_episodes(conn):
    assert list_sync.level_progress(conn, _season(conn, "z-list01")) == 2
    assert list_sync.level_progress(conn, _season(conn, "z-list02")) == 1  # not 3


def test_progress_is_pushed_per_level(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(anilist_client, "save_media_list_entry",
                        lambda token, media_id, **kw: sent.append((media_id, kw)) or kw)
    list_sync.push_progress_for_show(conn, "s-list01")
    assert sorted(sent) == [(101, {"progress": 2}), (102, {"progress": 1})]
    sent.clear()
    list_sync.push_progress_for_show(conn, "s-list01")
    assert sent == []  # unchanged since the lists agreed


def test_skipped_is_never_pushed(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(anilist_client, "save_media_list_entry",
                        lambda token, media_id, **kw: sent.append(media_id) or kw)
    conn.execute("UPDATE season SET status = 'skipped' WHERE id = 'z-list02'")
    list_sync.push(conn, "z-list02")
    assert sent == []  # R4.6


def test_auto_planned_then_skipped_is_deleted_from_both_lists(conn, monkeypatch):
    deleted = []
    monkeypatch.setattr(anilist_client, "fetch_my_list_entry_id", lambda token, aid: 9000 + aid)
    monkeypatch.setattr(anilist_client, "delete_media_list_entry",
                        lambda token, entry, **kw: deleted.append(("anilist", entry)))
    monkeypatch.setattr(mal_client, "delete_my_list_status",
                        lambda token, mid: deleted.append(("mal", mid)))
    conn.execute("UPDATE season SET status = 'skipped', mal_id = 555 WHERE id = 'z-list02'")
    list_sync.delete_if_auto_skipped(conn, "z-list02", "planned")
    assert deleted == [("anilist", 9102), ("mal", 555)]  # R2.10


def test_a_season_you_set_is_never_deleted(conn, monkeypatch):
    monkeypatch.setattr(anilist_client, "fetch_my_list_entry_id",
                        lambda *a: pytest.fail("must not delete"))
    conn.execute("UPDATE season SET status = 'skipped', status_set_manually = 1"
                 " WHERE id = 'z-list02'")
    list_sync.delete_if_auto_skipped(conn, "z-list02", "planned")
