"""The AniDB drip queue (user 2026-09-30): the backlog first — watching, planned, the rest —
then weekly refreshes of watching/planned anime; 200 a day; after a ban, nothing is asked
for a day. (Before: the queue read an episode-mapping table that is empty on prod, so
nothing was ever fetched.)"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anidb, config, util

NOW = "2026-09-30T10:00:00Z"


@pytest.fixture(autouse=True)
def _config():
    config.set_current(config.Config())
    anidb._banned_until = 0.0
    yield
    anidb._banned_until = 0.0


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "drip.db"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=Path(__file__).resolve().parent.parent,
                   env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
                   check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _anime(c, sid, status, tvdb, anidb_ids, space="anime"):
    c.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
              " status, tracked, created_at, updated_at)"
              " VALUES (?, 'episodic', ?, ?, 'romaji', ?, 1, 'x', 'x')", (sid, space, sid, status))
    c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
              " VALUES (?, 'tvdb', ?, '', 'x')", (sid, str(tvdb)))
    for aid in anidb_ids:
        c.execute("INSERT INTO anime_list_entry (anidb_id, tvdb_id, source, fetched_at)"
                  " VALUES (?, ?, 'test', 'x')", (aid, str(tvdb)))


def _have(c, aid, at):
    c.execute("INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, fetched_at)"
              " VALUES (?, 1, 1, ?)", (aid, at))


def test_backlog_by_status_then_weekly_refresh_of_watching_and_planned(conn):
    _anime(conn, "s-other1", "completed", 1, [30, 31])
    _anime(conn, "s-plan01", "planned", 2, [20])
    _anime(conn, "s-watch1", "watching", 3, [10, 11])
    _anime(conn, "s-tvshow", "watching", 4, [99], space="tv")  # not anime: never asked
    _have(conn, 11, "2026-09-01T00:00:00Z")  # watching, old → refresh
    _have(conn, 31, "2026-09-01T00:00:00Z")  # completed show, old → left alone
    conn.commit()
    assert anidb.drip_queue(conn, 10, NOW) == [10, 20, 30, 11]
    _have(conn, 12, NOW)
    conn.execute("UPDATE anidb_episode SET fetched_at = ? WHERE anidb_anime_id = 11", (NOW,))
    assert anidb.drip_queue(conn, 10, NOW) == [10, 20, 30]  # fresh data waits a week


def test_a_ban_stops_every_request_for_a_day(conn, monkeypatch):
    _anime(conn, "s-watch1", "watching", 3, [10, 11])
    conn.commit()
    calls = []

    def fake(aid, client=None):
        calls.append(aid)
        return "BANNED"

    monkeypatch.setattr(anidb, "fetch_anime_episodes", fake)
    monkeypatch.setattr(anidb, "_banned_until", 0.0)
    assert anidb.drip_fetch_episodes(conn, limit=5)["banned"] is True
    assert anidb.drip_fetch_episodes(conn, limit=5)["fetched"] == 0
    assert calls == [10]  # one request, then nothing until the back-off ends


def test_a_not_found_marker_moves_its_date_so_a_refresh_never_loops(conn):
    anidb._write_tombstone(conn, 7, "2026-09-01T00:00:00Z")
    anidb._write_tombstone(conn, 7, NOW)
    at = conn.execute("SELECT fetched_at FROM anidb_episode WHERE anidb_anime_id = 7").fetchone()
    assert at[0] == NOW


def test_refreshes_count_toward_the_daily_cap(conn, monkeypatch):
    today = util.now_utc_iso()
    _anime(conn, "s-watch1", "watching", 3, [10])
    conn.executemany(
        "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, fetched_at)"
        " VALUES (?, 1, 1, ?)", [(1000 + i, today) for i in range(anidb.ANIDB_DAILY_CAP)])
    conn.commit()
    monkeypatch.setattr(anidb, "fetch_anime_episodes", lambda *a, **k: pytest.fail("asked"))
    assert anidb.drip_fetch_episodes(conn, limit=5)["fetched"] == 0
