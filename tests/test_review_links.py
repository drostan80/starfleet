"""The links a review carries (10-06): AniList, MAL, TVDB and Sonarr pages of what it is about."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import review_links, reviews

NOW = "2026-10-06T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "links.db"
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
        " VALUES ('s-link01', 'episodic', 'anime', 'L', 'romaji', 'planned', 1, ?, ?)",
        (NOW, NOW))
    c.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id, mal_id, source,"
        " created_at, updated_at) VALUES ('z-link01', 's-link01', 1, 'planned', 111, 222,"
        " 'manual', ?, ?)", (NOW, NOW))
    for service, ext, url in (("tvdb", "71634", ""),
                              ("sonarr", "some-show", "http://192.168.1.77:8989/series/some-show")):
        c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                  " VALUES ('s-link01', ?, ?, ?, ?)", (service, ext, url, NOW))
    c.commit()
    yield c
    c.close()


def _links(conn, entity_type, entity_id, field="anilist_id", payload=None, show_id=None):
    reviews.open_review(conn, entity_type, entity_id, field, "anilist", "x", ["acknowledge"],
                        payload or {}, show_id=show_id)
    row = conn.execute("SELECT * FROM pending_review WHERE entity_id = ?", (entity_id,)).fetchone()
    return {link["service"]: link for link in review_links.for_review(conn, dict(row))}


def test_a_season_review_links_its_own_ids_and_the_shows_tvdb_and_sonarr(conn):
    links = _links(conn, "season", "z-link01")
    assert list(links) == ["anilist", "mal", "tvdb", "sonarr"]
    assert links["anilist"]["url"] == "https://anilist.co/anime/111"
    assert links["mal"]["url"] == "https://myanimelist.net/anime/222"
    assert links["tvdb"]["url"] == "https://thetvdb.com/dereferrer/series/71634"
    assert links["sonarr"]["url"] == "http://192.168.1.77:8989/series/some-show"


def test_an_add_check_review_links_the_candidates_ids_not_the_shows(conn):
    payload = {"show_id": "s-link01", "season_id": None, "tvdb_id": 71634, "anilist_id": 435,
               "mal_id": 435}
    links = _links(conn, "show", "anilist:435", "add_check:needs_user", payload, "s-link01")
    assert links["anilist"]["url"] == "https://anilist.co/anime/435"
    assert links["mal"]["url"] == "https://myanimelist.net/anime/435"
    assert links["tvdb"]["id"] == "71634"
    assert links["sonarr"]["label"] == "Sonarr"


def test_a_review_about_nothing_known_has_no_links(conn):
    assert _links(conn, "show", "anilist:999", "add_check:needs_user") == {}


def test_a_season_without_the_ids_on_the_row_uses_its_external_id_rows(conn):
    conn.execute("UPDATE season SET anilist_id = NULL WHERE id = 'z-link01'")
    conn.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                 " VALUES ('z-link01', 'anilist', '333', ?)", (NOW,))
    conn.commit()
    assert _links(conn, "season", "z-link01")["anilist"]["id"] == "333"


