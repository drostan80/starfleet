"""PLAN-CODE 9.0 — external writes captured instead of sent."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lcars import (
    anilist_client,
    config,
    db,
    external_writes,
    list_baseline,
    mal_client,
    sonarr_client,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "lcars_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True, capture_output=True,
    )
    c = db.connect(db_path)
    config.set_current(config.Config(
        anilist_access_token="atok", mal_access_token="mtok",
        sonarr_url="http://sonarr:8989", sonarr_api_key="k",
    ))
    monkeypatch.delenv("LCARS_EXTERNAL_WRITES", raising=False)  # the real default
    yield c
    db.close()


def _no_http(monkeypatch):
    """Any real HTTP call fails the test."""
    def refuse(*a, **kw):
        raise AssertionError("an external write was sent")
    monkeypatch.setattr(httpx.Client, "send", refuse)


def _pending(conn):
    return [dict(r) for r in external_writes.pending(conn)]


def test_capture_is_the_default_and_only_send_lets_writes_out(conn, monkeypatch):
    assert config.Config().external_writes == "capture"
    assert external_writes.capturing()
    config.get_current().external_writes = "send"
    assert not external_writes.capturing()
    monkeypatch.setenv("LCARS_EXTERNAL_WRITES", "capture")  # the env wins
    assert external_writes.capturing()


def test_list_writes_are_captured_merged_and_never_agreed(conn, monkeypatch):
    _no_http(monkeypatch)
    list_baseline.anilist_save(conn, "atok", 101, status="CURRENT")
    list_baseline.anilist_save(conn, "atok", 101, progress=3)
    list_baseline.mal_save(conn, "mtok", 202, num_watched_episodes=3)
    anilist_client.delete_media_list_entry("atok", 999, anilist_id=103)
    mal_client.delete_my_list_status("mtok", 204)
    rows = _pending(conn)
    assert [(r["service"], r["op"]) for r in rows] == [
        ("anilist", "save"), ("mal", "save"), ("anilist", "delete"), ("mal", "delete")]
    assert json.loads(rows[0]["args"]) == {"anilist_id": 101, "status": "CURRENT", "progress": 3}
    assert "atok" not in json.dumps(rows) and "mtok" not in json.dumps(rows)
    # nothing was sent, so nothing is agreed: the next reconcile still sees the gap
    assert conn.execute("SELECT COUNT(*) FROM list_baseline").fetchone()[0] == 0


def test_the_mal_token_refresh_is_blocked(conn, monkeypatch):
    _no_http(monkeypatch)
    with pytest.raises(mal_client.MALError, match="blocked"):
        mal_client.refresh_access_token("cid", None, "rtok")


def test_sonarr_writes_are_captured_with_no_fake_link(conn, monkeypatch):
    _no_http(monkeypatch)
    with sonarr_client.SonarrClient("http://sonarr:8989", "k") as client:
        created = client.add_series({"tvdbId": 424536, "title": "Frieren", "titleSlug": "f"})
        client.update_series({"id": 7, "monitored": False})
        client.monitor_episodes([1, 2], True)
        client.monitor_episodes([3], False)
        client.delete_series(7, delete_files=False)
    assert created["id"] is None and created["titleSlug"] is None
    ops = [r["op"] for r in _pending(conn)]
    assert ops == ["POST series", "PUT series/7", "PUT episode/monitor",
                   "PUT episode/monitor", "DELETE series/7"]


def test_send_sends_in_order_records_what_was_agreed_and_keeps_failures(conn, monkeypatch):
    list_baseline.anilist_save(conn, "atok", 101, status="CURRENT")
    mal_client.delete_my_list_status("mtok", 204)
    conn.commit()
    sent = []
    monkeypatch.setattr(anilist_client, "save_media_list_entry",
                        lambda token, aid, **kw: sent.append(("anilist", aid, kw)) or kw)

    def fail(token, mal_id, client=None):
        raise mal_client.MALError("MAL down")
    monkeypatch.setattr(mal_client, "delete_my_list_status", fail)

    result = external_writes.send_pending(conn, limit=10)

    assert result == {"sent": 1, "failed": 1, "left": 1}
    assert sent == [("anilist", 101, {"status": "CURRENT"})]
    assert conn.execute("SELECT status FROM list_baseline WHERE service = 'anilist'"
                        " AND external_id = 101").fetchone()[0] == "watching"
    left = _pending(conn)[0]
    assert (left["op"], left["send_error"]) == ("delete", "MAL down")
    # sending never switches capture off for anything else
    assert external_writes.capturing()


def test_send_respects_the_limit(conn, monkeypatch):
    for aid in (1, 2, 3):
        list_baseline.anilist_save(conn, "atok", aid, status="PLANNING")
    conn.commit()
    monkeypatch.setattr(anilist_client, "save_media_list_entry", lambda token, aid, **kw: kw)
    assert external_writes.send_pending(conn, limit=2) == {"sent": 2, "failed": 0, "left": 1}
