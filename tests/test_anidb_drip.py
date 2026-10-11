"""The AniDB drip queue (user 2026-09-30): the backlog first — watching, planned, the rest —
then refreshes of watching/planned anime, spread evenly (a day's target = in-scope anime / 7,
user 2026-10-11); 200 a day; after a ban, nothing is asked for a day. (Before: the queue read an
episode-mapping table that is empty on prod, so nothing was ever fetched.)"""

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
    assert anidb.drip_queue(conn, 10, NOW) == [10, 20, 30]  # fresh data waits


def _many(c, n, status="watching", start=0):
    base = 0 if status == "watching" else 5000
    for i in range(start, start + n):
        _anime(c, f"s-{status[0]}{i:05}", status, 1000 + i + base, [2000 + i + base])


def test_the_daily_refresh_target_is_the_in_scope_anime_over_a_week(conn):
    assert anidb.refresh_target(conn) == 0
    _many(conn, 400)
    _many(conn, 23, "planned")
    _anime(conn, "s-done01", "completed", 9, [9001])  # out of scope: not counted
    _anime(conn, "s-tvshow", "watching", 4, [99], space="tv")
    conn.commit()
    assert anidb.refresh_target(conn) == 61  # 423 / 7
    _many(conn, 77, "planned", start=23)
    conn.commit()
    assert anidb.refresh_target(conn) == 72  # 500 / 7, rounded up


def test_refreshes_are_watching_first_then_oldest_and_not_before_the_floor(conn):
    _anime(conn, "s-w-old1", "watching", 1, [1])
    _anime(conn, "s-w-new1", "watching", 2, [2])
    _anime(conn, "s-p-old1", "planned", 3, [3])
    _anime(conn, "s-wfresh", "watching", 4, [4])
    _have(conn, 1, "2026-09-10T00:00:00Z")
    _have(conn, 2, "2026-09-22T00:00:00Z")  # 8 days old: due
    _have(conn, 3, "2026-09-01T00:00:00Z")  # planned and oldest, but after the watching ones
    _have(conn, 4, "2026-09-25T00:00:00Z")  # 5 days old: under the floor
    conn.commit()
    assert anidb.drip_queue(conn, 10, NOW) == [1, 2, 3]
    conn.execute("UPDATE anidb_episode SET fetched_at = '2026-09-24T00:00:00Z'"
                 " WHERE anidb_anime_id = 4")  # 6 days and 10 h: over the floor
    assert anidb.drip_queue(conn, 10, NOW) == [1, 2, 4, 3]


def test_the_refresh_room_caps_refreshes_and_the_backlog_uses_it(conn):
    _anime(conn, "s-new001", "watching", 1, [1])  # no data: backlog
    for i in range(2, 6):
        _anime(conn, f"s-old{i:03}", "watching", i, [i])
        _have(conn, i, f"2026-09-0{i}T00:00:00Z")
    conn.commit()
    assert anidb.drip_queue(conn, 10, NOW) == [1, 2, 3, 4, 5]
    assert anidb.drip_queue(conn, 10, NOW, refresh_room=3) == [1, 2, 3]  # backlog took one
    assert anidb.drip_queue(conn, 10, NOW, refresh_room=0) == [1]  # backlog is never held back
    assert anidb.drip_queue(conn, 2, NOW, refresh_room=3) == [1, 2]


def test_the_drip_refreshes_no_more_than_the_days_target(conn, monkeypatch):
    for i in range(1, 8):  # 7 watching anime → a target of 1 a day
        _anime(conn, f"s-w{i:05}", "watching", i, [i])
        _have(conn, i, f"2026-09-0{i}T00:00:00Z")
    conn.commit()
    asked = []

    def fake(aid, client=None):
        asked.append(aid)
        return []

    monkeypatch.setattr(anidb, "fetch_anime_episodes", fake)
    assert anidb.refresh_target(conn) == 1
    anidb.drip_fetch_episodes(conn, limit=5)
    anidb.drip_fetch_episodes(conn, limit=5)
    assert asked == [1]  # the oldest; the second tick finds the day's allowance used


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
