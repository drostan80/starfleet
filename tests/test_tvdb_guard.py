"""tvdb_guard (R1.14a, user 2026-09-30): every automatic TVDB id goes through one gate —
one show per TVDB id (also enforced by the database), never a series id on a film, a
single source is a review (R3.7e), never a blind link; propagation no longer writes
AniList/MAL ids on the show (R1.23)."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anidb, config, reviews, shows, tvdb_guard


@pytest.fixture(autouse=True)
def _config():
    config.set_current(config.Config())
    yield


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "guard.db"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=Path(__file__).resolve().parent.parent,
                   env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
                   check=True, capture_output=True)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _show(c, sid, shape="episodic", space="anime"):
    c.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
              " status, tracked, created_at, updated_at)"
              " VALUES (?, ?, ?, ?, 'romaji', 'planned', 1, 'x', 'x')", (sid, shape, space, sid))


def _ext(c, sid, service, value):
    c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
              " VALUES (?, ?, ?, '', 'x')", (sid, service, str(value)))


def _tvdb(c, sid):
    row = c.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND"
                    " service = 'tvdb'", (sid,)).fetchone()
    return row[0] if row else None


def _open_reviews(c, sid):
    return c.execute("SELECT * FROM pending_review WHERE entity_id = ? AND field = ? AND"
                     " resolved_at IS NULL", (sid, tvdb_guard.REVIEW_FIELD)).fetchall()


def test_a_single_source_opens_a_review_and_writes_nothing(conn):
    _show(conn, "s-aaaaaa")
    assert tvdb_guard.offer(conn, "s-aaaaaa", 123, "anime-lists") == tvdb_guard.REVIEW
    assert _tvdb(conn, "s-aaaaaa") is None
    [r] = _open_reviews(conn, "s-aaaaaa")
    assert json.loads(r["payload"]) == {"tvdb_id": "123", "source": "anime-lists"}
    # asked again: the same review, not a second one
    tvdb_guard.offer(conn, "s-aaaaaa", 123, "anime-lists")
    assert len(_open_reviews(conn, "s-aaaaaa")) == 1


def test_confirmed_writes_but_never_a_second_show_or_a_film_or_an_overwrite(conn):
    _show(conn, "s-aaaaaa")
    _show(conn, "s-bbbbbb")
    _show(conn, "s-film01", shape="movie")
    assert tvdb_guard.offer(conn, "s-aaaaaa", 123, "sonarr", confirmed=True) == tvdb_guard.WRITTEN
    assert tvdb_guard.offer(conn, "s-bbbbbb", 123, "sonarr", confirmed=True) == tvdb_guard.HELD
    assert tvdb_guard.offer(conn, "s-film01", 999, "wikidata", confirmed=True) == tvdb_guard.FILM
    assert tvdb_guard.offer(conn, "s-aaaaaa", 456, "sonarr", confirmed=True) == tvdb_guard.EXISTS
    got = (_tvdb(conn, "s-aaaaaa"), _tvdb(conn, "s-bbbbbb"), _tvdb(conn, "s-film01"))
    assert got == ("123", None, None)
    assert _open_reviews(conn, "s-film01") == []


def test_your_answers_yes_no_and_another_id(conn):
    for sid, value in (("s-yes001", 1), ("s-no0001", 2), ("s-other1", 3)):
        _show(conn, sid)
        tvdb_guard.offer(conn, sid, value, "fribb")
    ids = {r["entity_id"]: r["id"] for r in conn.execute(
        "SELECT * FROM pending_review WHERE field = ?", (tvdb_guard.REVIEW_FIELD,))}
    reviews.resolve_choice(conn, ids["s-yes001"], tvdb_guard.CONFIRM, "data", None)
    reviews.resolve_choice(conn, ids["s-no0001"], tvdb_guard.REJECT, "data", None)
    reviews.resolve_choice(conn, ids["s-other1"], tvdb_guard.OTHER, "data", "it is 77")
    assert (_tvdb(conn, "s-yes001"), _tvdb(conn, "s-no0001"), _tvdb(conn, "s-other1")) == (
        "1", None, "77")
    # a "no" is remembered: the same suggestion doesn't come back
    tvdb_guard.offer(conn, "s-no0001", 2, "fribb")
    assert _open_reviews(conn, "s-no0001") == []


def test_another_id_already_held_keeps_the_review_open(conn):
    _show(conn, "s-aaaaaa")
    _show(conn, "s-bbbbbb")
    _ext(conn, "s-aaaaaa", "tvdb", 77)
    tvdb_guard.offer(conn, "s-bbbbbb", 5, "fribb")
    [r] = _open_reviews(conn, "s-bbbbbb")
    with pytest.raises(reviews.ReviewError):
        reviews.resolve_choice(conn, r["id"], tvdb_guard.OTHER, "data", "77")
    assert _open_reviews(conn, "s-bbbbbb") != []


def test_propagation_writes_no_list_ids_on_the_show_and_no_blind_tvdb(conn):
    """The 09-30 incident: 1,052 AniList + MAL ids on shows, 54 series ids on films."""
    _show(conn, "s-anime1")
    _ext(conn, "s-anime1", "anidb", 10)
    _show(conn, "s-film01", shape="movie")
    _ext(conn, "s-film01", "anidb", 20)
    for anidb_id, tvdb_id in ((10, 555), (20, 72933)):
        conn.execute("INSERT INTO anime_list_entry (anidb_id, tvdb_id, source, fetched_at)"
                     " VALUES (?, ?, 'test', 'x')", (anidb_id, str(tvdb_id)))
    fribb_dataset = [{"anidb_id": 10, "anilist_id": 1000, "mal_id": 2000},
                     {"anidb_id": 20, "anilist_id": 3000, "mal_id": 4000}]
    counts = anidb.propagate_cross_ids(conn, fribb_dataset)
    services = {r[0] for r in conn.execute("SELECT service FROM show_external_id")}
    assert "anilist" not in services and "mal" not in services and "tvdb" not in services
    assert counts["tvdb"] == 0
    assert len(_open_reviews(conn, "s-anime1")) == 1  # the anime: yours to confirm
    assert _open_reviews(conn, "s-film01") == []  # a film never takes a series id


def test_a_tracked_series_needs_its_tvdb_id_and_a_film_never_takes_one():
    base = {"tracking_space": "anime", "primary_title": "romaji", "title_romaji": "X"}
    with pytest.raises(shows.ShowInputError, match="R1.14a"):
        shows._check_tvdb_rules({**base, "media_shape": "episodic"})
    with pytest.raises(shows.ShowInputError, match="R3.2a"):
        shows._check_tvdb_rules({**base, "media_shape": "movie", "tvdb_id": 1})
    shows._check_tvdb_rules({**base, "media_shape": "episodic", "tvdb_id": 1})
    shows._check_tvdb_rules({**base, "media_shape": "episodic", "no_tvdb_ok": True})
    shows._check_tvdb_rules({**base, "media_shape": "movie", "tmdb_id": 1})


def test_a_film_tmdb_id_from_fribb_is_a_number_never_a_list(conn):
    """09-30: Fribb gives films as {"movie": [4935]}; it was stored as "[4935]"."""
    from lcars import fribb
    assert fribb.tmdb_scalar([4935]) == 4935
    assert fribb.tmdb_scalar([1, 2]) is None
    assert fribb.tmdb_scalar(77) == 77
    _show(conn, "s-howl01", shape="movie")
    _ext(conn, "s-howl01", "anidb", 30)
    conn.execute("INSERT INTO anime_list_entry (anidb_id, tvdb_id, tmdb_tv, source, fetched_at)"
                 " VALUES (30, 'movie', 9999, 'test', 'x')")
    dataset = [{"anidb_id": 30, "anilist_id": 431, "themoviedb_id": {"movie": [4935]}}]
    anidb.propagate_cross_ids(conn, dataset)
    [row] = conn.execute("SELECT external_id, url FROM show_external_id WHERE show_id ="
                         " 's-howl01' AND service = 'tmdb'").fetchall()
    assert tuple(row) == ("4935", "https://www.themoviedb.org/movie/4935")  # not tv/9999


def test_a_film_never_takes_a_series_tmdb_id_from_fribb(conn):
    """09-30 dry run: Time of Eve: The Movie took the series' tv/60937 from Fribb."""
    _show(conn, "s-eve001", shape="movie")
    _ext(conn, "s-eve001", "anidb", 40)
    dataset = [{"anidb_id": 40, "anilist_id": 9, "themoviedb_id": {"tv": 60937}}]
    anidb.propagate_cross_ids(conn, dataset)
    assert conn.execute("SELECT 1 FROM show_external_id WHERE show_id = 's-eve001'"
                        " AND service = 'tmdb'").fetchone() is None
