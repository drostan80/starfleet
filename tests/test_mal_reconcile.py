"""MAL → LCARS reverse sync (mal_reconcile.py, 2026-08-26). Exercised
against a real migrated SQLite DB (same reasoning as test_availability),
with mal_client.fetch_my_list and the onward-push clients monkeypatched —
no real network. Status + progress only (score reverse-sync isn't built).
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, mal_client, mal_reconcile


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "mal_reconcile_test.db"
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


@pytest.fixture(autouse=True)
def _config():
    cfg = config.Config()
    cfg.mal_access_token = "mal-tok"
    cfg.anilist_access_token = "ani-tok"  # so onward push to AniList is live
    config.set_current(cfg)
    yield
    config.set_current(config.Config())


def _add_show(conn, show_id, status="planned"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', 'Test', 'romaji', ?, 1, 'x', 'x')",
        (show_id, status),
    )


def _add_season(conn, season_id, show_id, season_number, mal_id, anilist_id=None):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, mal_id, anilist_id, source, matched,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'manual', 1, 'x', 'x')",
        (season_id, show_id, season_number, mal_id, anilist_id),
    )
    # S3: _apply_remote_list reads from season_external_id, not season.{mal_id,anilist_id} —
    # mirror into the table so test fixtures are found by the reconciler.
    if mal_id is not None:
        conn.execute(
            "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
            " VALUES (?, 'mal', ?, 'x')",
            (season_id, mal_id),
        )
    if anilist_id is not None:
        conn.execute(
            "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
            " VALUES (?, 'anilist', ?, 'x')",
            (season_id, anilist_id),
        )


def _add_episode(conn, ep_id, show_id, season, episode, state="unwatched", air_date=None):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state, air_date_utc,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'regular', ?, ?, 'x', 'x')",
        (ep_id, show_id, season, episode, state, air_date),
    )


def _mal_list(monkeypatch, entries):
    monkeypatch.setattr(mal_client, "fetch_my_list", lambda token: entries)


def _capture_anilist(monkeypatch):
    calls = []
    monkeypatch.setattr(
        anilist_client,
        "save_media_list_entry",
        lambda token, media_id, **kw: calls.append({"media_id": media_id, **kw}),
    )
    return calls


def test_applies_status_and_progress_from_mal_and_pushes_onward_to_anilist(conn, monkeypatch):
    _add_show(conn, "s-000001", status="planned")
    _add_season(conn, "z-000001", "s-000001", 1, mal_id=101, anilist_id=201)
    for n in (1, 2, 3):
        _add_episode(conn, f"e-00000{n}", "s-000001", 1, n, air_date="2020-01-01T00:00:00Z")
    conn.commit()
    _mal_list(monkeypatch, [{"mal_id": 101, "status": "watching", "num_watched_episodes": 2}])
    anilist_calls = _capture_anilist(monkeypatch)

    result = mal_reconcile.reconcile_mal_progress(conn)

    assert result["shows_status_updated"] == 1
    assert result["episodes_backfilled"] == 2
    assert conn.execute("SELECT status FROM show WHERE id='s-000001'").fetchone()[0] == "watching"
    states = {
        r["episode"]: r["state"]
        for r in conn.execute("SELECT episode, state FROM episode WHERE show_id='s-000001'")
    }
    assert states == {1: "watched", 2: "watched", 3: "unwatched"}
    # onward push to AniList fired: status (CURRENT) + progress (2)
    assert {"media_id": 201, "status": "CURRENT"} in anilist_calls
    assert {"media_id": 201, "progress": 2} in anilist_calls


def test_unaired_episode_is_never_marked_or_completed(conn, monkeypatch):
    _add_show(conn, "s-000002", status="watching")
    _add_season(conn, "z-000002", "s-000002", 1, mal_id=102, anilist_id=202)
    _add_episode(conn, "e-0000a1", "s-000002", 1, 1, air_date="2020-01-01T00:00:00Z")
    _add_episode(conn, "e-0000b1", "s-000002", 1, 2, air_date="2099-01-01T00:00:00Z")  # far future
    conn.commit()
    # MAL claims completed through ep 2, but ep 2 hasn't aired.
    _mal_list(monkeypatch, [{"mal_id": 102, "status": "completed", "num_watched_episodes": 2}])
    _capture_anilist(monkeypatch)

    mal_reconcile.reconcile_mal_progress(conn)

    states = {
        r["episode"]: r["state"]
        for r in conn.execute("SELECT episode, state FROM episode WHERE show_id='s-000002'")
    }
    assert states == {1: "watched", 2: "unwatched"}  # ep 2 not fabricated
    # not force-completed while an episode is still unaired
    assert conn.execute("SELECT status FROM show WHERE id='s-000002'").fetchone()[0] == "watching"


def test_duplicate_mal_id_is_excluded_and_flagged(conn, monkeypatch):
    _add_show(conn, "s-0003aa")
    _add_show(conn, "s-0003bb")
    _add_season(conn, "z-0003aa", "s-0003aa", 1, mal_id=103)
    _add_season(conn, "z-0003bb", "s-0003bb", 1, mal_id=103)  # same mal_id
    conn.commit()
    _mal_list(monkeypatch, [{"mal_id": 103, "status": "watching", "num_watched_episodes": 1}])
    _capture_anilist(monkeypatch)

    result = mal_reconcile.reconcile_mal_progress(conn)

    assert result["ambiguous_mal_id_conflicts"] == 2
    assert result["shows_status_updated"] == 0  # neither applied
    flagged = conn.execute(
        "SELECT COUNT(*) FROM pending_review WHERE field = 'mal_id_conflict'"
    ).fetchone()[0]
    assert flagged == 2


def test_no_mal_token_is_a_clean_noop(conn, monkeypatch):
    config.get_current().mal_access_token = None
    _add_show(conn, "s-000004")
    _add_season(conn, "z-000004", "s-000004", 1, mal_id=104)
    conn.commit()
    # fetch_my_list must never be called when unconfigured
    monkeypatch.setattr(
        mal_client, "fetch_my_list", lambda token: (_ for _ in ()).throw(AssertionError("called"))
    )
    result = mal_reconcile.reconcile_mal_progress(conn)
    assert result["seasons_checked"] == 0


def test_no_status_change_when_already_matching_and_no_onward_push(conn, monkeypatch):
    # Converged state: MAL already agrees with LCARS -> nothing changes,
    # nothing pushed onward (loop-prevention: only real diffs propagate).
    _add_show(conn, "s-000005", status="watching")
    _add_season(conn, "z-000005", "s-000005", 1, mal_id=105, anilist_id=205)
    _add_episode(conn, "e-0000x1", "s-000005", 1, 1, state="watched")
    conn.commit()
    _mal_list(monkeypatch, [{"mal_id": 105, "status": "watching", "num_watched_episodes": 1}])
    anilist_calls = _capture_anilist(monkeypatch)

    result = mal_reconcile.reconcile_mal_progress(conn)

    assert result["shows_status_updated"] == 0
    assert result["episodes_backfilled"] == 0
    assert anilist_calls == []  # converged -> no onward push, no oscillation
