"""LCARS as the hub between AniList and MAL (list_baseline.py, 2026-09-25).

Each test pins a failure of the old "the list always wins" reconcile: a
failed push was overwritten by the list's old value, and any disagreement
between the two lists (or two LCARS seasons on one entry) ping-ponged."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import (
    anilist_client,
    config,
    list_baseline,
    mal_client,
    mal_reconcile,
    resolvers,
    watch_reconcile,
)

PAST = "2020-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "list_baseline.db"
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
    config.set_current(config.Config(anilist_access_token="a", mal_access_token="m"))
    return c


@pytest.fixture
def lists(monkeypatch):
    """Fake AniList + MAL: `state` holds each list's entries, writes land in
    it (AniList completes an entry itself when progress reaches its episode
    count), `calls` records every write, `fail` makes writes raise."""
    state = {"anilist": {}, "mal": {}, "episodes": {}}
    calls = []
    fail = {"anilist": False, "mal": False}

    def al_save(token, media_id, **kw):
        if fail["anilist"]:
            raise anilist_client.AniListError("down")
        calls.append(("anilist", media_id, kw))
        e = state["anilist"].setdefault(media_id, {"status": "PLANNING", "progress": 0})
        e.update({k: v for k, v in kw.items() if k in ("status", "progress")})
        if state["episodes"].get(media_id) and e["progress"] >= state["episodes"][media_id]:
            e["status"] = "COMPLETED"
        return {"id": 1, **e}

    def mal_save(token, mal_id, **kw):
        if fail["mal"]:
            raise mal_client.MALError("down")
        calls.append(("mal", mal_id, kw))
        e = state["mal"].setdefault(mal_id, {"status": "plan_to_watch", "progress": 0})
        if "status" in kw:
            e["status"] = kw["status"]
        if "num_watched_episodes" in kw:
            e["progress"] = kw["num_watched_episodes"]
        return {"status": e["status"], "num_episodes_watched": e["progress"]}

    monkeypatch.setattr(anilist_client, "save_media_list_entry", al_save)
    monkeypatch.setattr(mal_client, "update_my_list_status", mal_save)
    monkeypatch.setattr(
        anilist_client, "fetch_my_anime_list",
        lambda token: [
            {"anilist_id": k, "status": v["status"], "progress": v["progress"], "format": "TV"}
            for k, v in state["anilist"].items()
        ],
    )
    monkeypatch.setattr(
        mal_client, "fetch_my_list",
        lambda token: [
            {"mal_id": k, "status": v["status"], "num_watched_episodes": v["progress"]}
            for k, v in state["mal"].items()
        ],
    )
    return state, calls, fail


def _season_show(conn, status="watching", season_status=None, episodes=2, watched=0):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-hub001', 'episodic', 'anime', 'T', 'romaji', ?, 1, 'x', 'x')",
        (status,),
    )
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id, mal_id,"
        " source, created_at, updated_at) VALUES"
        " ('z-hub001', 's-hub001', 1, ?, 100, 200, 'fribb', 'x', 'x')",
        (season_status,),
    )
    for service, ext in (("anilist", 100), ("mal", 200)):
        conn.execute(
            "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
            " VALUES ('z-hub001', ?, ?, 'x')",
            (service, ext),
        )
    for n in range(1, episodes + 1):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, state, air_date_utc,"
            " created_at, updated_at) VALUES (?, 's-hub001', 1, ?, 'regular', ?, ?, 'x', 'x')",
            (f"e-hub00{n}", n, "watched" if n <= watched else "unwatched", PAST),
        )
    conn.commit()


def _season_status(conn):
    return conn.execute("SELECT status FROM season WHERE id = 'z-hub001'").fetchone()[0]


def _run_both(conn):
    watch_reconcile.reconcile_watch_progress(conn)
    mal_reconcile.reconcile_mal_progress(conn)


def test_first_run_seeds_agreement_without_writing(conn, lists):
    state, calls, _ = lists
    _season_show(conn, season_status="watching")
    state["anilist"][100] = {"status": "CURRENT", "progress": 0}
    state["mal"][200] = {"status": "watching", "progress": 0}

    _run_both(conn)

    assert calls == []
    assert list_baseline.get(conn, "anilist", 100)["status"] == "watching"
    assert list_baseline.get(conn, "mal", 200)["status"] == "watching"


def test_first_run_disagreement_lcars_wins(conn, lists):
    state, calls, _ = lists
    _season_show(conn, season_status="completed", watched=2)
    state["anilist"][100] = {"status": "DROPPED", "progress": 2}

    watch_reconcile.reconcile_watch_progress(conn)

    assert _season_status(conn) == "completed"
    assert state["anilist"][100]["status"] == "COMPLETED"


def test_a_failed_push_is_retried_not_overwritten_by_the_list(conn, lists):
    # Old behaviour: LCARS set paused, the push failed, the next reconcile
    # read the list's old value and put LCARS back.
    state, calls, fail = lists
    _season_show(conn, season_status="watching")
    state["anilist"][100] = {"status": "CURRENT", "progress": 0}
    watch_reconcile.reconcile_watch_progress(conn)  # seed

    conn.execute("UPDATE season SET status = 'paused' WHERE id = 'z-hub001'")
    fail["anilist"] = True
    resolvers._push_season_status(
        conn, dict(conn.execute("SELECT * FROM season WHERE id='z-hub001'").fetchone()), "paused"
    )
    conn.commit()
    watch_reconcile.reconcile_watch_progress(conn)
    assert _season_status(conn) == "paused"  # not reverted to watching

    fail["anilist"] = False
    watch_reconcile.reconcile_watch_progress(conn)
    assert _season_status(conn) == "paused"
    assert state["anilist"][100]["status"] == "PAUSED"  # the retry landed


def test_a_list_edit_reaches_lcars_and_the_other_list_once(conn, lists):
    state, calls, _ = lists
    _season_show(conn, season_status="watching")
    state["anilist"][100] = {"status": "CURRENT", "progress": 0}
    state["mal"][200] = {"status": "watching", "progress": 0}
    _run_both(conn)  # seed
    assert calls == []

    state["mal"][200]["status"] = "on_hold"  # edited on MAL (e.g. phone app)
    _run_both(conn)
    assert _season_status(conn) == "paused"
    assert state["anilist"][100]["status"] == "PAUSED"

    calls.clear()
    for _ in range(3):
        _run_both(conn)
    assert calls == []  # converged: nothing bounces


def test_anilist_completing_an_entry_itself_is_not_an_edit(conn, lists):
    # AniList sets COMPLETED on its own when progress reaches the episode
    # count; the wrapper records what AniList saved, so the next reconcile
    # doesn't read it as a list edit and write it back into LCARS.
    state, calls, _ = lists
    _season_show(conn, season_status="watching", episodes=2)
    state["episodes"][100] = 2
    state["anilist"][100] = {"status": "CURRENT", "progress": 0}
    watch_reconcile.reconcile_watch_progress(conn)  # seed

    conn.execute("UPDATE episode SET state = 'watched' WHERE show_id = 's-hub001'")
    season = dict(conn.execute("SELECT * FROM season WHERE id='z-hub001'").fetchone())
    resolvers._push_season_progress(conn, season)
    conn.commit()
    assert state["anilist"][100]["status"] == "COMPLETED"
    assert list_baseline.get(conn, "anilist", 100)["status"] == "completed"


def test_an_lcars_unwatch_is_pushed_not_re_marked_from_the_list(conn, lists):
    state, calls, _ = lists
    _season_show(conn, season_status="watching", watched=1)
    state["anilist"][100] = {"status": "CURRENT", "progress": 1}
    watch_reconcile.reconcile_watch_progress(conn)  # seed: progress 1 agreed

    conn.execute("UPDATE episode SET state = 'unwatched' WHERE show_id = 's-hub001'")
    conn.commit()
    watch_reconcile.reconcile_watch_progress(conn)

    ep = conn.execute("SELECT state FROM episode WHERE id = 'e-hub001'").fetchone()[0]
    assert ep == "unwatched"
    assert state["anilist"][100]["progress"] == 0
