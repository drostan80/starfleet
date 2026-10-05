"""Syoboi runs per level, matched to episodes by air date (syoboi_levels.py, user 10-05)."""

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import provisional_episodes as pe
from lcars import syoboi_levels as sl

ROOT = Path(__file__).resolve().parent.parent
T = "2026-10-05T00:00:00Z"
BASE = dt.datetime(2026, 1, 5, 13, 0)


def stamp(day):
    return (BASE + dt.timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "sl.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " tracked, created_at, updated_at)"
        " VALUES ('s-sl0001', 'episodic', 'anime', 'Show', 'romaji', 'watching', 1, ?, ?)", (T, T))
    c.commit()
    return c


def level(conn, zid, number, first_day, count, kind="tvdb_season", parent=None, part=1,
          abs_from=None, anilist=None, episodes=True):
    """A level whose `count` episodes air weekly from `first_day` (days after BASE)."""
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id, anilist_id,"
        " status, source, created_at, updated_at) VALUES (?, 's-sl0001', ?, ?, ?, ?, ?,"
        " 'watching', 'manual', ?, ?)", (zid, number, part, kind, parent, anilist, T, T))
    if abs_from is not None:
        conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                     (zid, abs_from, abs_from + count - 1))
    owner = parent or zid
    for i in range(count if episodes else 0):
        n = i + 1
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, sonarr_season,"
            " sonarr_episode, kind, absolute_number, air_date_utc, air_date_source, state,"
            " provisional, created_at, updated_at) VALUES (?, 's-sl0001', ?, ?, ?, ?, ?,"
            " 'regular', ?, ?, 'sonarr', 'unwatched', 0, ?, ?)",
            (f"e-{zid[2:5]}{i:03d}", number, owner, n, number, n,
             (abs_from if abs_from is not None else 1) + i, stamp(first_day + 7 * i), T, T))
    conn.commit()


def run(conn, tid, counts, first_day, chid=19):
    """Syoboi's run `tid`: counts 1..counts weekly from `first_day`."""
    for n in range(1, counts + 1):
        conn.execute(
            "INSERT INTO syoboi_program (pid, tid, chid, count, st_time_utc, ed_time_utc, deleted,"
            " fetched_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (tid * 100 + n + chid * 100000, tid, chid, n, stamp(first_day + 7 * (n - 1)),
             stamp(first_day + 7 * (n - 1)), T))
    conn.commit()


def level_tid(conn, zid, tid):
    conn.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                 " VALUES (?, 'syoboi', ?, ?)", (zid, str(tid), T))
    conn.commit()


def episode_numbers(conn, mapping):
    return {r["episode"] + 100 * r["season"]: mapping.get(r["id"]) for r in conn.execute(
        "SELECT id, season, episode FROM episode WHERE show_id = 's-sl0001'")}


def test_each_season_takes_the_run_that_fits_it_not_the_shows_stored_one(conn):
    level(conn, "z-s10000", 1, 0, 6)
    level(conn, "z-s20000", 2, 300, 6)
    run(conn, 100, 6, 0)
    run(conn, 200, 6, 300)
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES ('s-sl0001', 'syoboi', '100', 'x', ?)", (T,))  # the first season's TID
    level_tid(conn, "z-s20000", 200)
    got = episode_numbers(conn, sl.episode_map(conn, "s-sl0001"))
    assert got[101] == (100, 1) and got[206] == (200, 6)
    # without the level id, the season-2 run is not even a candidate: nothing is guessed
    conn.execute("DELETE FROM season_external_id")
    assert episode_numbers(conn, sl.episode_map(conn, "s-sl0001"))[201] is None


def test_one_tid_spanning_two_levels_is_split_by_air_date(conn):
    level(conn, "z-s10000", 1, 0, 4)       # cour A: episodes 1-4
    level(conn, "z-s20000", 2, 28, 4)      # cour B starts four weeks later
    run(conn, 300, 8, 0)                   # one Syoboi run counting 1..8 across both
    level_tid(conn, "z-s10000", 300)
    level_tid(conn, "z-s20000", 300)
    got = episode_numbers(conn, sl.episode_map(conn, "s-sl0001"))
    assert got[101] == (300, 1) and got[201] == (300, 5) and got[204] == (300, 8)


def test_a_part_continues_the_season_numbering_but_the_run_restarts_at_one(conn):
    level(conn, "z-s20000", 2, 0, 0)  # the TVDB season: 12 episodes, two cours
    for n in range(12):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, kind, absolute_number,"
            " air_date_utc, air_date_source, state, provisional, created_at, updated_at)"
            " VALUES (?, 's-sl0001', 2, 'z-s20000', ?, 'regular', ?, ?, 'sonarr', 'unwatched', 0,"
            " ?, ?)", (f"e-par{n:03d}", n + 1, 25 + n, stamp(7 * n), T, T))
    level(conn, "z-p10000", 2, 0, 6, kind="part", parent="z-s20000", part=1, abs_from=25,
          episodes=False)
    level(conn, "z-p20000", 2, 42, 6, kind="part", parent="z-s20000", part=2, abs_from=31,
          episodes=False)
    run(conn, 500, 6, 0)
    run(conn, 600, 6, 42)  # part two's own run restarts at 1
    level_tid(conn, "z-p10000", 500)
    level_tid(conn, "z-p20000", 600)
    got = {r["episode"]: sl.episode_map(conn, "s-sl0001").get(r["id"]) for r in conn.execute(
        "SELECT id, episode FROM episode WHERE show_id = 's-sl0001'")}
    assert got[1] == (500, 1) and got[6] == (500, 6)
    assert got[7] == (600, 1) and got[12] == (600, 6)


def test_a_run_more_than_three_days_off_matches_nothing(conn):
    level(conn, "z-s10000", 1, 0, 4)
    run(conn, 100, 4, 10)  # ten days later: another run
    level_tid(conn, "z-s10000", 100)
    assert sl.episode_map(conn, "s-sl0001") == {}


def test_level_ids_are_seeded_from_arm_through_the_anilist_id_once(conn):
    level(conn, "z-s10000", 1, 0, 2, anilist=11)
    level(conn, "z-s20000", 2, 100, 2, anilist=22)
    level(conn, "z-s30000", 3, 200, 2, anilist=33)  # ARM has nothing for 33
    assert sl.seed_level_ids(conn, {11: 100, 22: 200}) == 2
    assert sl.seed_level_ids(conn, {11: 100, 22: 999}) == 0  # never replaces
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT season_id, external_id, url FROM season_external_id WHERE service = 'syoboi'")}
    assert rows["z-s20000"] == ("200", "https://cal.syoboi.jp/tid/200")
    assert sl.fetchable_tids(conn) == [100, 200]
    conn.execute("UPDATE show SET status = 'completed'")
    assert sl.fetchable_tids(conn) == []  # only watching/planned shows are fetched per level


def test_provisional_episodes_continue_the_levels_own_run(conn):
    level(conn, "z-s10000", 1, 0, 6)
    level(conn, "z-s20000", 2, 300, 2)  # TVDB lists two episodes of season 2
    run(conn, 100, 6, 0)
    run(conn, 200, 12, 300)
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES ('s-sl0001', 'syoboi', '100', 'x', ?)", (T,))
    level_tid(conn, "z-s20000", 200)
    made = pe.sync_show(conn, "s-sl0001", stamp(300 + 7))  # episode 2 has aired
    assert made["created"] == 3
    rows = conn.execute("SELECT season, episode, air_date_utc FROM episode WHERE provisional = 1"
                        " ORDER BY episode").fetchall()
    assert [(r["season"], r["episode"]) for r in rows] == [(2, 3), (2, 4), (2, 5)]
    assert rows[0]["air_date_utc"] == stamp(300 + 14)  # run 200's third broadcast


def test_an_empty_air_date_is_filled_from_the_levels_run(conn):
    level(conn, "z-s10000", 1, 0, 4)
    run(conn, 100, 4, 0)
    level_tid(conn, "z-s10000", 100)
    conn.execute("UPDATE episode SET air_date_utc = NULL, air_date_source = NULL"
                 " WHERE episode = 3")
    assert sl.fill_gaps(conn) == 1
    row = conn.execute("SELECT air_date_utc, air_date_source FROM episode WHERE episode = 3"
                       ).fetchone()
    assert tuple(row) == (stamp(14), "syoboi")
