"""tvdb_backfill.py — Fribb reverse-lookup tvdb_id backfill, 2026-08-18.
Same real-migrated-SQLite-DB + fake-dataset approach test_service_
presence.py already established (a real fribb.load_dataset() network
call is monkeypatched, same as test_server.py's own reconcileSeasonMapping
tests do).
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import fribb, tvdb_backfill


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "tvdb_backfill_test.db"
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


def _add_show(conn, show_id, title_romaji="Test Show", media_shape="episodic", tracked=True):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, ?, 'anime', ?, 'romaji', 'watching', ?, 'x', 'x')",
        (show_id, media_shape, title_romaji, int(tracked)),
    )
    conn.commit()


def _link_anilist(conn, show_id, anilist_id):
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'anilist', ?, ?, 'x')",
        (show_id, str(anilist_id), f"https://anilist.co/anime/{anilist_id}"),
    )
    conn.commit()


def _tvdb_link(conn, show_id):
    return conn.execute(
        "SELECT * FROM show_external_id WHERE show_id = ? AND service = 'tvdb'", (show_id,)
    ).fetchone()


def test_backfills_a_real_tvdb_id_from_a_known_anilist_id(conn, monkeypatch):
    _add_show(conn, "s-tvb001")
    _link_anilist(conn, "s-tvb001", 20613)
    monkeypatch.setattr(
        fribb, "load_dataset", lambda: [{"anilist_id": 20613, "tvdb_id": 279328}]
    )

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 1
    link = _tvdb_link(conn, "s-tvb001")
    assert link is not None
    assert link["external_id"] == "279328"
    assert link["url"] == "https://thetvdb.com/dereferrer/series/279328"


def test_skips_a_show_that_already_has_a_tvdb_link(conn, monkeypatch):
    _add_show(conn, "s-tvb002")
    _link_anilist(conn, "s-tvb002", 20613)
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-tvb002', 'tvdb', '1', 'https://thetvdb.com/dereferrer/series/1', 'x')"
    )
    conn.commit()
    called = []
    monkeypatch.setattr(
        fribb, "load_dataset", lambda: called.append(1) or [{"anilist_id": 20613, "tvdb_id": 2}]
    )

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 0
    assert called == []  # never even fetched the dataset — nothing to resolve
    assert _tvdb_link(conn, "s-tvb002")["external_id"] == "1"  # untouched


def test_skips_a_show_with_no_anilist_id_at_all(conn, monkeypatch):
    _add_show(conn, "s-tvb003")  # never linked to AniList
    monkeypatch.setattr(fribb, "load_dataset", lambda: [{"anilist_id": 1, "tvdb_id": 100}])

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 0
    assert _tvdb_link(conn, "s-tvb003") is None


def test_skips_a_movie_shaped_show(conn, monkeypatch):
    _add_show(conn, "s-tvb004", media_shape="movie")
    _link_anilist(conn, "s-tvb004", 999)
    monkeypatch.setattr(fribb, "load_dataset", lambda: [{"anilist_id": 999, "tvdb_id": 1}])

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 0
    assert _tvdb_link(conn, "s-tvb004") is None


def test_leaves_a_show_alone_when_fribb_cant_resolve_it_unambiguously(conn, monkeypatch):
    # A genuinely ambiguous Fribb entry (a franchise split across
    # multiple tvdb_ids) — never guess, real presence check confirms
    # the candidate was seen but nothing was written for it.
    _add_show(conn, "s-tvb005")
    _link_anilist(conn, "s-tvb005", 20613)
    monkeypatch.setattr(
        fribb,
        "load_dataset",
        lambda: [
            {"anilist_id": 20613, "tvdb_id": 1},
            {"anilist_id": 20613, "tvdb_id": 2},
        ],
    )

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 0
    assert _tvdb_link(conn, "s-tvb005") is None


def test_skips_an_untracked_show(conn, monkeypatch):
    _add_show(conn, "s-tvb006", tracked=False)
    _link_anilist(conn, "s-tvb006", 20613)
    monkeypatch.setattr(
        fribb, "load_dataset", lambda: [{"anilist_id": 20613, "tvdb_id": 279328}]
    )

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 0
    assert _tvdb_link(conn, "s-tvb006") is None


def test_no_candidates_never_even_loads_the_dataset(conn, monkeypatch):
    # No episodic show with a known anilist_id and no tvdb link at all —
    # the early return should skip fribb.load_dataset() entirely (no
    # pointless network/cache-read cost on an all-caught-up library).
    called = []
    monkeypatch.setattr(fribb, "load_dataset", lambda: called.append(1) or [])

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 0
    assert called == []


def test_multiple_candidates_resolved_in_one_pass(conn, monkeypatch):
    _add_show(conn, "s-tvb007", title_romaji="Show A")
    _link_anilist(conn, "s-tvb007", 1)
    _add_show(conn, "s-tvb008", title_romaji="Show B")
    _link_anilist(conn, "s-tvb008", 2)
    monkeypatch.setattr(
        fribb,
        "load_dataset",
        lambda: [{"anilist_id": 1, "tvdb_id": 100}, {"anilist_id": 2, "tvdb_id": 200}],
    )

    updated = tvdb_backfill.backfill_tvdb_ids(conn)
    assert updated == 2
    assert _tvdb_link(conn, "s-tvb007")["external_id"] == "100"
    assert _tvdb_link(conn, "s-tvb008")["external_id"] == "200"
