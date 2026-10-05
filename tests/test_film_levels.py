"""film_levels.py — list-only AniList films get an episode so the numbering places them (R1.4)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, episode_movie_link, film_levels, numbering

NOW = "2026-10-05T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "films.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-film01', 'episodic', 'anime', 'Film Show', 'english', 'watching', 1, ?, ?)",
        (NOW, NOW),
    )
    for zid, n in (("z-film01", 1), ("z-film02", 2)):
        c.execute(
            "INSERT INTO season (id, show_id, season_number, source, status, created_at,"
            " updated_at) VALUES (?, 's-film01', ?, 'manual', 'completed', ?, ?)",
            (zid, n, NOW, NOW))
    for i, (s, e, day) in enumerate([(1, 1, "2015-01-01"), (1, 2, "2015-01-08"),
                                     (2, 1, "2017-01-01"), (2, 2, "2017-01-08")]):
        c.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc,"
            " state, created_at, updated_at)"
            " VALUES (?, 's-film01', ?, ?, 'regular', ?, 'watched', ?, ?)",
            (f"e-reg{i:03d}", s, e, f"{day}T12:00:00Z", NOW, NOW))
    c.commit()
    yield c
    c.close()


def _film_level(c, zid="z-lvl001", anilist=111, status="completed", show="s-film01"):
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, source, status,"
        " list_sync, created_at, updated_at)"
        " VALUES (?, ?, NULL, 'special', ?, 'auto', ?, 1, ?, ?)",
        (zid, show, anilist, status, NOW, NOW))
    c.commit()


def _dataset(anilist=111, anidb=900, type_="MOVIE"):
    return [{"anilist_id": anilist, "anidb_id": anidb, "type": type_, "season": {"tvdb": 0}}]


def _anidb_data(c, anidb=900, airdate="2016-06-01", length=100):
    c.execute("INSERT INTO anidb_anime (anidb_id, main_title, fetched_at)"
              " VALUES (?, 'The Film', ?)", (anidb, NOW))
    c.execute("INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate,"
              " length_minutes, fetched_at) VALUES (?, 1, 1, ?, ?, ?)",
              (anidb, airdate, length, NOW))
    c.commit()


def test_a_completed_list_only_film_gets_a_watched_film_episode_from_anidb(conn):
    _film_level(conn)
    _anidb_data(conn)
    result = film_levels.create_film_episodes(conn, _dataset())
    assert result == {"created": 1, "waiting": 0}
    ep = conn.execute("SELECT * FROM episode WHERE season_id = 'z-lvl001'").fetchone()
    assert (ep["kind"], ep["season"], ep["episode"]) == (
        "bonus_movie", 0, film_levels.FILM_EPISODE_BASE)
    assert (ep["air_date_utc"], ep["air_date_source"], ep["runtime_minutes"]) == (
        "2016-06-01T00:00:00Z", "anidb", 100)
    assert ep["title"] == "The Film" and ep["state"] == "watched"
    assert ep["sonarr_season"] is None  # nothing Sonarr knows
    assert conn.execute("SELECT COUNT(*) FROM watch_event WHERE show_id = 's-film01' AND season = 0"
                        ).fetchone()[0] == 1
    again = film_levels.create_film_episodes(conn, _dataset())
    assert again["created"] == 0  # the level has its episode now


def test_a_planned_film_is_unwatched_and_has_no_watch_event(conn):
    _film_level(conn, status="planned")
    _anidb_data(conn)
    film_levels.create_film_episodes(conn, _dataset())
    ep = conn.execute("SELECT state FROM episode WHERE season_id = 'z-lvl001'").fetchone()
    assert ep["state"] == "unwatched"
    assert conn.execute("SELECT COUNT(*) FROM watch_event WHERE season = 0").fetchone()[0] == 0


@pytest.mark.parametrize("status,kind,type_", [
    ("skipped", "special", "MOVIE"),   # R2.10: a skipped level is not followed
    ("completed", "special", "OVA"),   # list-only OVAs/specials stay as they are
    ("completed", "special", "TV"),
])
def test_only_followed_films_are_given_an_episode(conn, status, kind, type_):
    _film_level(conn, status=status)
    _anidb_data(conn)
    assert film_levels.create_film_episodes(conn, _dataset(type_=type_))["created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM episode WHERE season_id = 'z-lvl001'"
                        ).fetchone()[0] == 0


def test_a_level_that_already_has_an_episode_or_a_child_is_left_alone(conn):
    _film_level(conn)  # z-lvl001: already holds an episode
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, season_id, created_at,"
        " updated_at) VALUES ('e-have01', 's-film01', 0, 5, 'special', 'z-lvl001', ?, ?)",
        (NOW, NOW))
    _film_level(conn, zid="z-lvl002", anilist=222)  # z-lvl002: has a child level
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, kind, parent_id, source, status,"
        " created_at, updated_at) VALUES ('z-lvl003', 's-film01', NULL, 'special', 'z-lvl002',"
        " 'auto', 'planned', ?, ?)", (NOW, NOW))
    conn.commit()
    _anidb_data(conn)
    _anidb_data(conn, anidb=901)
    result = film_levels.create_film_episodes(conn, _dataset() + _dataset(222, 901))
    assert result["created"] == 0


def test_a_film_level_that_already_has_a_place_in_the_numbering_is_left_alone(conn):
    # e.g. a film Sonarr files as a special and AniDB's mapping already numbered: it has a span
    _film_level(conn)
    conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES ('z-lvl001', 3, 3)")
    conn.commit()
    _anidb_data(conn)
    assert film_levels.create_film_episodes(conn, _dataset())["created"] == 0


def test_without_anidb_data_anilist_supplies_the_date_and_length(conn, monkeypatch):
    _film_level(conn, status="completed")
    monkeypatch.setattr(anilist_client, "fetch_media_facts", lambda _id: {
        "startDate": {"year": 1978, "month": 7, "day": 22}, "duration": 35,
        "title": {"english": "Riddle of the Arcadia", "romaji": "Arcadia no Nazo"}})
    result = film_levels.create_film_episodes(conn, _dataset(anidb=None))
    ep = conn.execute("SELECT * FROM episode WHERE season_id = 'z-lvl001'").fetchone()
    assert result["created"] == 1
    assert (ep["air_date_utc"], ep["air_date_source"], ep["runtime_minutes"], ep["title"]) == (
        "1978-07-22T00:00:00Z", "anilist", 35, "Riddle of the Arcadia")


def test_when_nobody_can_say_anything_the_film_waits_for_a_later_pass(conn, monkeypatch):
    _film_level(conn)

    def down(_id):
        raise anilist_client.AniListError("down")

    monkeypatch.setattr(anilist_client, "fetch_media_facts", down)
    assert film_levels.create_film_episodes(conn, _dataset(anidb=None)) == {
        "created": 0, "waiting": 1}


def test_a_year_only_film_gets_a_placeholder_number_not_a_guessed_date(conn, monkeypatch):
    _film_level(conn)
    monkeypatch.setattr(anilist_client, "fetch_media_facts", lambda _id: {
        "startDate": {"year": None}, "duration": 90, "title": {"romaji": "Film"}})
    film_levels.create_film_episodes(conn, _dataset(anidb=None))
    ep = conn.execute("SELECT air_date_utc, air_date_source FROM episode"
                      " WHERE season_id = 'z-lvl001'").fetchone()
    assert (ep["air_date_utc"], ep["air_date_source"]) == (None, None)


def test_the_numbering_places_the_film_between_the_seasons_in_its_own_existing_level(conn):
    _film_level(conn)
    _anidb_data(conn, airdate="2016-06-01", length=100)  # between S1 (2015) and S2 (2017)
    film_levels.create_film_episodes(conn, _dataset())
    levels_before = conn.execute("SELECT COUNT(*) FROM season").fetchone()[0]

    for _ in range(3):  # Memory Alpha runs this every ~20 minutes
        numbering.renumber_show(conn, "s-film01")

    ep = conn.execute(
        "SELECT absolute_number FROM episode WHERE season_id = 'z-lvl001'").fetchone()
    s2 = conn.execute(
        "SELECT absolute_number FROM episode WHERE show_id = 's-film01' AND season = 2"
        " AND episode = 1").fetchone()
    assert ep["absolute_number"] == 3 and float(ep["absolute_number"]).is_integer()  # R1.4
    assert s2["absolute_number"] == 4  # S2 moved up by the film (R1.2a)
    spans = conn.execute("SELECT abs_from, abs_to FROM season_span WHERE season_id = 'z-lvl001'"
                         ).fetchall()
    assert [tuple(r) for r in spans] == [(3, 3)]  # the level that holds the AniList id
    assert conn.execute("SELECT COUNT(*) FROM season").fetchone()[0] == levels_before  # no twin


def test_a_short_film_is_still_a_whole_number_not_a_mini(conn, monkeypatch):
    _film_level(conn)
    monkeypatch.setattr(anilist_client, "fetch_media_facts", lambda _id: {
        "startDate": {"year": 2016, "month": 6, "day": 1}, "duration": 35,
        "title": {"romaji": "Short Film"}})
    film_levels.create_film_episodes(conn, _dataset(anidb=None))
    numbering.renumber_show(conn, "s-film01")
    ep = conn.execute(
        "SELECT absolute_number FROM episode WHERE season_id = 'z-lvl001'").fetchone()
    assert float(ep["absolute_number"]).is_integer()


def test_the_movie_link_sweep_ignores_a_list_only_film_that_is_its_own_level(conn):
    _film_level(conn)
    _anidb_data(conn)
    film_levels.create_film_episodes(conn, _dataset())
    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["unmatched"] == 0 and result["flagged"] == 0
    assert conn.execute("SELECT COUNT(*) FROM episode_movie_link").fetchone()[0] == 0


def test_two_films_before_any_episode_each_keep_their_own_level_and_span(conn):
    # Captain Harlock on the prod copy: no main episodes yet; the numbering must not read the
    # second film's made-up coordinates as the first film's AniDB entry.
    conn.execute("DELETE FROM episode")
    conn.execute("DELETE FROM season WHERE season_number IS NOT NULL")
    _film_level(conn, zid="z-lvl001", anilist=111)
    _film_level(conn, zid="z-lvl002", anilist=222)
    _anidb_data(conn, anidb=900, airdate="1978-07-22", length=35)
    _anidb_data(conn, anidb=901, airdate="2013-09-07", length=115)
    film_levels.create_film_episodes(conn, _dataset(111, 900) + _dataset(222, 901))
    assert conn.execute("SELECT COUNT(*) FROM episode_anidb_mapping").fetchone()[0] == 2
    for _ in range(2):
        numbering.renumber_show(conn, "s-film01")
    for zid in ("z-lvl001", "z-lvl002"):
        spans = conn.execute("SELECT abs_from, abs_to FROM season_span WHERE season_id = ?",
                             (zid,)).fetchall()
        assert len(spans) == 1, zid
    assert conn.execute("SELECT COUNT(*) FROM season WHERE show_id = 's-film01'").fetchone()[0] == 2
