"""Every external id has a link; Syoboi ids fill from season-level AniList ids (user 10-05)."""

import sqlite3
from unittest import mock

import pytest

from lcars import arm, external_urls


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE show (id TEXT PRIMARY KEY, tracked INTEGER DEFAULT 1,
                           tracking_space TEXT DEFAULT 'anime',
                           media_shape TEXT DEFAULT 'episodic');
        CREATE TABLE show_external_id (show_id TEXT, service TEXT, external_id TEXT, url TEXT,
                                       created_at TEXT, UNIQUE (show_id, service));
        CREATE TABLE season (id TEXT PRIMARY KEY, show_id TEXT, season_number INTEGER,
                             part_number INTEGER DEFAULT 1, kind TEXT, anilist_id INTEGER);
        INSERT INTO show VALUES ('s-001', 1, 'anime', 'episodic');
        INSERT INTO show VALUES ('s-002', 1, 'anime', 'movie');
    """)
    return c


def test_missing_links_are_filled_and_stored_ones_kept(conn):
    conn.executescript("""
        INSERT INTO show_external_id VALUES ('s-001', 'syoboi', '8015', NULL, '');
        INSERT INTO show_external_id VALUES ('s-001', 'anidb', '19942', '', '');
        INSERT INTO show_external_id VALUES ('s-001', 'tvmaze', '94497', NULL, '');
        INSERT INTO show_external_id VALUES ('s-001', 'tvdb', '475306', 'https://x/kept', '');
        INSERT INTO show_external_id VALUES ('s-001', 'tvmaze2', '1', NULL, '');
        INSERT INTO show_external_id VALUES ('s-002', 'tmdb', '77', NULL, '');
        INSERT INTO show_external_id VALUES ('s-001', 'imdb', '-1', NULL, '');
    """)
    assert external_urls.fill_missing(conn) == 4
    urls = {(r["show_id"], r["service"]): r["url"]
            for r in conn.execute("SELECT * FROM show_external_id")}
    assert urls[("s-001", "syoboi")] == "https://cal.syoboi.jp/tid/8015"
    assert urls[("s-001", "anidb")] == "https://anidb.net/anime/19942"
    assert urls[("s-001", "tvmaze")] == "https://www.tvmaze.com/shows/94497"
    assert urls[("s-001", "tvdb")] == "https://x/kept"
    assert urls[("s-002", "tmdb")] == "https://www.themoviedb.org/movie/77"
    assert urls[("s-001", "tvmaze2")] is None and urls[("s-001", "imdb")] is None


def test_a_show_takes_the_syoboi_id_of_its_latest_mapped_season(conn):
    conn.executescript("""
        INSERT INTO season VALUES ('z-1', 's-001', 1, 1, 'tvdb_season', 11);
        INSERT INTO season VALUES ('z-2', 's-001', 2, 1, 'tvdb_season', 22);
        INSERT INTO season VALUES ('z-3', 's-001', 3, 1, 'tvdb_season', 33);
    """)
    with mock.patch.object(arm, "_al_to_syoboi_cache", {}):
        n = arm.seed_syoboi_external_ids(conn, [
            {"anilist_id": 11, "syobocal_tid": 100}, {"anilist_id": 22, "syobocal_tid": 200}])
    assert n == 1  # season 3 has no Syoboi entry in ARM: season 2's is the latest mapped
    row = conn.execute("SELECT external_id, url FROM show_external_id"
                       " WHERE service = 'syoboi'").fetchone()
    assert (row["external_id"], row["url"]) == ("200", "https://cal.syoboi.jp/tid/200")
    with mock.patch.object(arm, "_al_to_syoboi_cache", {}):
        assert arm.seed_syoboi_external_ids(conn, [{"anilist_id": 11, "syobocal_tid": 100}]) == 0
