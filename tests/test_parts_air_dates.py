"""AniList's air-date reconcile for a TVDB season split into parts (cours), each with its own
AniList entry (Kusuriya no Hitorigoto S3, 10-05): AniList's episode N is the N-th episode of the
PART, and the part's count — not the whole season's — is what AniList's count is compared with."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, metadata, util

NOW = "2026-10-05T00:00:00Z"
P1 = 1760000000  # a Thursday, 2025-10-09
WEEK = 7 * 86400


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "parts.db"
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
        " VALUES ('s-prt001', 'episodic', 'anime', 'P', 'romaji', 'watching', 1, ?, ?)",
        (NOW, NOW))
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, status, source, created_at,"
        " updated_at) VALUES ('z-prt000', 's-prt001', 3, 'tvdb_season', 'watching', 'manual',"
        " ?, ?)", (NOW, NOW))
    parts = (("z-prt001", 1, 195516, 49, 60), ("z-prt002", 2, 200927, 61, 72))
    for zid, part, al, lo, hi in parts:
        c.execute(
            "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
            " anilist_id, source, status, created_at, updated_at)"
            " VALUES (?, 's-prt001', 3, ?, 'part', 'z-prt000', ?, 'manual', 'watching', ?, ?)",
            (zid, part, al, NOW, NOW))
        c.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                  (zid, lo, hi))
    for n in range(1, 25):
        c.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number,"
            " air_date_utc, air_date_source, state, season_id, created_at, updated_at)"
            " VALUES (?, 's-prt001', 3, ?, 'regular', ?, NULL, NULL, 'unwatched', 'z-prt000',"
            " ?, ?)",
            (f"e-prt{n:03d}", n, 48 + n, NOW, NOW))
    c.commit()
    config.set_current(config.Config())
    yield c
    c.close()


def _schedule(monkeypatch, per_entry):
    def fake(anilist_id, *a, **kw):
        start = per_entry[anilist_id]
        if start is None:
            return None
        nodes = [{"episode": n, "airingAt": start + (n - 1) * WEEK} for n in range(1, 13)]
        return {"episodes": 12, "nodes": nodes}

    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", fake)


def test_each_part_gets_its_own_schedule_on_its_own_episodes_and_no_review(conn, monkeypatch):
    _schedule(monkeypatch, {195516: P1, 200927: P1 + 20 * WEEK})
    metadata._reconcile_air_dates(conn, {"id": "s-prt001"})

    dates = {r["episode"]: r["air_date_utc"] for r in conn.execute(
        "SELECT episode, air_date_utc FROM episode WHERE show_id = 's-prt001'")}
    assert dates[1] == util.unix_to_iso(P1)
    assert dates[12] == util.unix_to_iso(P1 + 11 * WEEK)
    assert dates[13] == util.unix_to_iso(P1 + 20 * WEEK)        # part 2's first, not part 1's
    assert dates[24] == util.unix_to_iso(P1 + 31 * WEEK)
    assert conn.execute("SELECT COUNT(*) FROM pending_review").fetchone()[0] == 0  # no split guard


def test_a_part_with_no_schedule_yet_leaves_its_episodes_alone(conn, monkeypatch):
    _schedule(monkeypatch, {195516: P1, 200927: None})  # part 2 announced, no dates
    metadata._reconcile_air_dates(conn, {"id": "s-prt001"})
    dates = {r["episode"]: r["air_date_utc"] for r in conn.execute(
        "SELECT episode, air_date_utc FROM episode WHERE show_id = 's-prt001'")}
    assert dates[1] is not None and dates[13] is None
