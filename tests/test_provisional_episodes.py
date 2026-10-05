"""Provisional episodes from Syoboi until TVDB takes over (provisional_episodes.py, 10-05).

Shape: a running anime whose TVDB list stops at E1-E2 while Syoboi numbers twelve weekly
broadcasts (So What's Wrong with Getting Reborn as a Goblin?, Syoboi 8015)."""

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import provisional_episodes as pe

ROOT = Path(__file__).resolve().parent.parent
T = "2026-10-05T00:00:00Z"
FIRST = dt.datetime(2026, 10, 5, 13, 0)  # Syoboi's E1, UTC
AFTER_E2 = "2026-10-12T14:00:00Z"  # E1 and E2 have aired


def stamp(week):
    return (FIRST + dt.timedelta(weeks=week)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "pv.db"
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
        " VALUES ('s-pv0001', 'episodic', 'anime', 'Goblin', 'romaji', 'watching', 1, ?, ?)",
        (T, T))
    c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
              " VALUES ('s-pv0001', 'syoboi', '8015', 'x', ?)", (T,))
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, status, source, created_at,"
        " updated_at) VALUES ('z-pv0001', 's-pv0001', 1, 'tvdb_season', 'watching', 'manual', ?,"
        " ?)", (T, T))
    for n in (1, 2):  # what TVDB/Sonarr lists
        c.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, sonarr_season,"
            " sonarr_episode, kind, air_date_utc, air_date_source, state, provisional, created_at,"
            " updated_at) VALUES (?, 's-pv0001', 1, 'z-pv0001', ?, 1, ?, 'regular', ?, 'sonarr',"
            " 'unwatched', 0, ?, ?)", (f"e-pv{n:04d}", n, n, stamp(n - 1), T, T))
    for n in range(1, 13):  # what Syoboi lists: twelve weekly broadcasts
        c.execute(
            "INSERT INTO syoboi_program (pid, tid, chid, count, st_time_utc, ed_time_utc, deleted,"
            " fetched_at) VALUES (?, 8015, 19, ?, ?, ?, 0, ?)",
            (1000 + n, n, stamp(n - 1), stamp(n - 1)[:11] + "13:30:00Z", T))
    c.commit()
    return c


def numbers(conn, provisional=1):
    return [r[0] for r in conn.execute(
        "SELECT episode FROM episode WHERE show_id = 's-pv0001' AND provisional = ? ORDER BY"
        " episode", (provisional,))]


def test_the_next_three_after_the_last_aired_are_added_dated_by_syoboi(conn):
    made = pe.sync_show(conn, "s-pv0001", AFTER_E2)
    assert made["created"] == 3 and numbers(conn) == [3, 4, 5]  # 2 aired + 3 ahead
    row = conn.execute("SELECT * FROM episode WHERE episode = 4").fetchone()
    assert (row["air_date_utc"], row["air_date_source"], row["state"], row["title"]) == (
        stamp(3), "syoboi", "unwatched", None)
    assert row["runtime_minutes"] == 30 and row["season_id"] == "z-pv0001"
    assert pe.sync_show(conn, "s-pv0001", AFTER_E2)["created"] == 0  # a second pass adds nothing


def test_the_window_moves_with_the_airing(conn):
    pe.sync_show(conn, "s-pv0001", AFTER_E2)
    pe.sync_show(conn, "s-pv0001", stamp(4) + "")  # E5 has now aired
    assert numbers(conn) == [3, 4, 5, 6, 7, 8]  # 5 aired + 3 ahead


def test_a_run_that_does_not_line_up_with_the_season_is_left_alone(conn):
    conn.execute("UPDATE syoboi_program SET st_time_utc ="
                 " strftime('%Y-%m-%dT%H:%M:%SZ', st_time_utc, '+40 days')")
    assert pe.sync_show(conn, "s-pv0001", AFTER_E2)["created"] == 0


def test_only_running_anime_that_are_watched_or_planned(conn):
    conn.execute("UPDATE show SET tracking_space = 'tv'")
    assert pe.sync_show(conn, "s-pv0001", AFTER_E2)["created"] == 0
    conn.execute("UPDATE show SET tracking_space = 'anime', status = 'completed'")
    assert pe.sync_show(conn, "s-pv0001", AFTER_E2)["created"] == 0


def test_anidb_caps_the_run_once_it_knows_how_many(conn):
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES ('s-pv0001', 'anidb', '19942', 'x', ?)", (T,))
    for n in range(1, 5):
        conn.execute("INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno,"
                     " fetched_at) VALUES (19942, 1, ?, ?)", (n, T))
    assert pe.sync_show(conn, "s-pv0001", AFTER_E2)["created"] == 2
    assert numbers(conn) == [3, 4]  # AniDB says four episodes


def test_tvdb_taking_over_adopts_the_row_and_keeps_a_watched_mark(conn):
    pe.sync_show(conn, "s-pv0001", AFTER_E2)
    conn.execute("UPDATE episode SET state = 'watched' WHERE episode = 3")
    taken = pe.adopt(conn, "s-pv0001", [
        {"seasonNumber": 1, "episodeNumber": 3, "airDateUtc": stamp(2)},
        {"seasonNumber": 1, "episodeNumber": 9, "airDateUtc": stamp(8)},  # no such row: ignored
    ])
    assert taken == 1
    row = conn.execute("SELECT * FROM episode WHERE episode = 3").fetchone()
    assert (row["provisional"], row["sonarr_season"], row["sonarr_episode"], row["state"]) == (
        0, 1, 3, "watched")
    assert row["air_date_raw_sonarr"] == stamp(2)
    pe.sync_show(conn, "s-pv0001", AFTER_E2)  # E4 and E5 are still beyond TVDB's last: kept
    assert numbers(conn) == [4, 5, 6]  # the window now starts after TVDB's E3


def test_a_provisional_episode_tvdb_skipped_goes_unless_it_was_watched(conn):
    pe.sync_show(conn, "s-pv0001", AFTER_E2)
    conn.execute("UPDATE episode SET state = 'watched' WHERE episode = 3")
    # TVDB lists E5 but never E3 or E4: the numbers at or below its last are not provisional now
    conn.execute("DELETE FROM episode WHERE episode = 5 AND provisional = 1")
    conn.execute(
        "INSERT INTO episode (id, show_id, season, season_id, episode, sonarr_season,"
        " sonarr_episode, kind, air_date_utc, air_date_source, state, provisional, created_at,"
        " updated_at) VALUES ('e-pv0005', 's-pv0001', 1, 'z-pv0001', 5, 1, 5, 'regular', ?,"
        " 'sonarr', 'unwatched', 0, ?, ?)", (stamp(4), T, T))
    made = pe.sync_show(conn, "s-pv0001", AFTER_E2)
    assert made["removed"] == 1 and made["kept_as_real"] == 1  # E4 removed, watched E3 stays
    assert numbers(conn, provisional=0) == [1, 2, 3, 5]


def test_never_more_than_six_beyond_what_tvdb_lists(conn):
    # a season that finished airing while TVDB still listed one placeholder
    conn.execute("DELETE FROM episode WHERE episode = 2")
    made = pe.sync_show(conn, "s-pv0001", "2027-06-01T00:00:00Z")  # all twelve have aired
    assert made["created"] == 6 and numbers(conn) == [2, 3, 4, 5, 6, 7]


def syoboi_candidates(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT e.episode, c.channel, c.air_date_utc FROM episode_air_candidate c"
        " JOIN episode e ON e.id = c.episode_id WHERE c.source = 'syoboi' ORDER BY 1, 2")]


def test_syoboi_is_a_choice_per_station_without_any_anidb_mapping(conn):
    from lcars import air_sources

    # a second station airs the same run a day later (the show has no AniDB mapping at all)
    for n in range(1, 13):
        conn.execute(
            "INSERT INTO syoboi_program (pid, tid, chid, count, st_time_utc, ed_time_utc, deleted,"
            " fetched_at) VALUES (?, 8015, 7, ?, ?, ?, 0, ?)",
            (2000 + n, n, stamp(n - 1).replace("T13:", "T14:").replace("-05T", "-06T")
             if n == 1 else stamp(n - 1), stamp(n - 1), T))
    air_sources.collect_candidates(conn, "s-pv0001")
    got = syoboi_candidates(conn)
    assert [(e, ch) for e, ch, _ in got] == [(1, "19"), (1, "7"), (2, "19"), (2, "7")]  # one each


def test_a_syoboi_run_that_does_not_fit_the_season_is_not_offered(conn):
    from lcars import air_sources

    conn.execute("UPDATE syoboi_program SET st_time_utc ="
                 " strftime('%Y-%m-%dT%H:%M:%SZ', st_time_utc, '+40 days')")
    air_sources.collect_candidates(conn, "s-pv0001")
    assert syoboi_candidates(conn) == []


def test_candidates_are_collected_for_airing_shows_without_a_button_click(conn):
    from lcars import air_sources

    first = air_sources.collect_for_airing(conn)
    assert first["shows"] == 1 and first["candidates"] > 0
    assert len(syoboi_candidates(conn)) == 2  # E1 and E2 on station 19
    assert air_sources.collect_for_airing(conn)["candidates"] == first["candidates"]  # idempotent
