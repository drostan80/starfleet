"""The Syoboi challenge guard (10-23): a Cloudflare bot challenge pauses every Syoboi call for
a while, opens one review, and the first normal answer afterwards closes it."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

from lcars import syoboi, syoboi_levels, util

URL = "https://cal.syoboi.jp/db.php"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "syoboi.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(syoboi, "_paused_until", None)
    monkeypatch.setattr(syoboi, "_rate_limit", lambda: None)
    monkeypatch.setattr(syoboi_levels, "fetchable_tids", lambda conn: [1])
    return c


def _client(*responses):
    client = mock.Mock()
    client.get.side_effect = list(responses)
    return client


def _challenge():
    return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="Just a moment...",
                          request=httpx.Request("GET", URL))


def _ok(body="<ProgLookupResponse><ProgItems/></ProgLookupResponse>"):
    return httpx.Response(200, text=body, request=httpx.Request("GET", URL))


def _open_reviews(conn):
    return conn.execute("SELECT * FROM pending_review WHERE field = ? AND resolved_at IS NULL",
                        (syoboi.REVIEW_FIELD,)).fetchall()


def test_challenge_pauses_and_opens_one_review(conn):
    client = _client(_challenge())
    with mock.patch.object(syoboi.httpx, "Client", return_value=client):
        result = syoboi.incremental_sync(conn)
    assert result["paused"] is True
    assert client.get.call_count == 1  # one request, not a batch per TID group
    reviews = _open_reviews(conn)
    assert len(reviews) == 1
    assert "automatically in 6 hours" in reviews[0]["proposed_value_chain"]
    assert conn.execute("SELECT value FROM web_setting WHERE key = 'syoboi_paused_until'"
                        ).fetchone()["value"] > util.now_utc_iso()


def test_paused_makes_no_request_and_no_second_review(conn):
    with mock.patch.object(syoboi.httpx, "Client", return_value=_client(_challenge())):
        syoboi.incremental_sync(conn)
    client = _client()
    with mock.patch.object(syoboi.httpx, "Client", return_value=client):
        result = syoboi.incremental_sync(conn)
    assert result["paused"] is True
    assert client.get.call_count == 0
    assert len(_open_reviews(conn)) == 1


def test_pause_survives_a_restart(conn, monkeypatch):
    with mock.patch.object(syoboi.httpx, "Client", return_value=_client(_challenge())):
        syoboi.incremental_sync(conn)
    monkeypatch.setattr(syoboi, "_paused_until", None)  # a new process
    client = _client()
    with mock.patch.object(syoboi.httpx, "Client", return_value=client):
        assert syoboi.incremental_sync(conn)["paused"] is True
    assert client.get.call_count == 0


def test_after_the_pause_a_normal_answer_closes_the_review(conn, monkeypatch):
    with mock.patch.object(syoboi.httpx, "Client", return_value=_client(_challenge())):
        syoboi.incremental_sync(conn)
    past = "2000-01-01T00:00:00Z"
    conn.execute("UPDATE web_setting SET value = ? WHERE key = 'syoboi_paused_until'", (past,))
    monkeypatch.setattr(syoboi, "_paused_until", past)
    client = _client(_ok(), _ok("<TitleLookupResponse><TitleItems/></TitleLookupResponse>"))
    with mock.patch.object(syoboi.httpx, "Client", return_value=client):
        result = syoboi.incremental_sync(conn)
    assert "paused" not in result
    assert client.get.call_count >= 1
    assert _open_reviews(conn) == []
    assert conn.execute("SELECT 1 FROM web_setting WHERE key = 'syoboi_paused_until'"
                        ).fetchone() is None


def test_challenge_again_after_recovery_is_a_new_review(conn, monkeypatch):
    for _ in range(2):
        with mock.patch.object(syoboi.httpx, "Client", return_value=_client(_challenge())):
            syoboi.incremental_sync(conn)
        past = "2000-01-01T00:00:00Z"
        conn.execute("UPDATE web_setting SET value = ? WHERE key = 'syoboi_paused_until'", (past,))
        monkeypatch.setattr(syoboi, "_paused_until", past)
        syoboi._recovered(conn)
    total = conn.execute("SELECT COUNT(*) FROM pending_review WHERE field = ?",
                         (syoboi.REVIEW_FIELD,)).fetchone()[0]
    assert total == 2


def test_other_403_is_not_a_challenge():
    plain = httpx.Response(403, text="forbidden", request=httpx.Request("GET", URL))
    assert syoboi._is_challenge(plain) is False
    assert syoboi._is_challenge(_challenge()) is True


def _tracked_show(conn, show_id, tid, status="watching"):
    show_id = f"s-{tid:06d}"
    conn.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
                 " status, tracked, created_at, updated_at)"
                 " VALUES (?, 'episodic', 'anime', 't', 'romaji', ?, 1, 'x', 'x')",
                 (show_id, status))
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES (?, 'syoboi', ?, 'u', 'x')", (show_id, str(tid)))


def test_429_pauses_without_sleeping_and_keeps_the_sweep(conn, monkeypatch):
    monkeypatch.setattr(syoboi_levels, "fetchable_tids", lambda conn: [])
    monkeypatch.setattr(syoboi.time, "sleep", lambda s: pytest.fail("slept"))
    _tracked_show(conn, "a", 1)
    r429 = httpx.Response(429, request=httpx.Request("GET", URL))
    client = _client(r429)
    with mock.patch.object(syoboi.httpx, "Client", return_value=client):
        result = syoboi.incremental_sync(conn)
    assert result["paused"] is True
    assert client.get.call_count == 1
    assert not _open_reviews(conn)  # a 429 is not a challenge: no review
    until = conn.execute("SELECT value FROM web_setting WHERE key = 'syoboi_paused_until'"
                         ).fetchone()["value"]
    assert util.now_utc_iso() < until < util.utc_iso_offset_hours(0.3)
    sweep = syoboi._load_sweep(conn)
    assert sweep["new"] == [1]  # the batch in flight is back at the head


def test_a_sweep_fetches_five_batches_a_tick_and_resumes(conn, monkeypatch):
    monkeypatch.setattr(syoboi_levels, "fetchable_tids", lambda conn: [])
    for i in range(1, 301):  # 300 never-fetched TIDs = 6 batches of 50
        _tracked_show(conn, f"s{i}", i)
    seen = []
    monkeypatch.setattr(syoboi, "_fetch_programs_all_tids",
                        lambda tids, **kw: seen.append(list(tids)) or [])
    monkeypatch.setattr(syoboi, "_fetch_and_ingest_titles", lambda *a, **k: None)
    syoboi.incremental_sync(conn)
    assert len(seen) == syoboi.MAX_BATCHES_PER_SWEEP
    assert len(syoboi._load_sweep(conn)["new"]) == 50
    syoboi.incremental_sync(conn)
    assert len(seen) == 6
    assert syoboi._load_sweep(conn) is None
    assert sorted(t for b in seen for t in b) == list(range(1, 301))


def test_priority_watching_first_then_soonest_broadcast(conn):
    _tracked_show(conn, "plan", 1, "planned")
    _tracked_show(conn, "w_later", 2)
    _tracked_show(conn, "w_today", 3)
    _tracked_show(conn, "w_none", 4)
    _tracked_show(conn, "w_tomorrow", 5)
    for tid, days in ((1, 0), (2, 3), (3, 0), (5, 1)):
        when = util.utc_iso_offset_hours(24 * days + 1)
        conn.execute("INSERT INTO syoboi_program (pid, tid, chid, st_time_utc, deleted, fetched_at)"
                     " VALUES (?, ?, 1, ?, 0, 'x')", (tid, tid, when))
    assert syoboi._priority_order(conn, [1, 2, 3, 4, 5]) == [3, 5, 2, 4, 1]
