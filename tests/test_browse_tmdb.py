"""Tests for the TV & Movies browse module (src/lcars/browse_tmdb.py)."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from lcars import browse_tmdb

# ── Fixtures ─────────────────────────────────────────────


def _make_tmdb_tv_result(tmdb_id, name="Test Show"):
    return {
        "id": tmdb_id,
        "name": name,
        "original_name": name,
        "overview": "A test show.",
        "poster_path": f"/poster{tmdb_id}.jpg",
        "backdrop_path": f"/bg{tmdb_id}.jpg",
        "first_air_date": "2026-09-01",
        "popularity": 100.0,
        "vote_average": 7.5,
        "genre_ids": [18, 80],
    }


def _make_tmdb_movie_result(tmdb_id, title="Test Movie"):
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "overview": "A test movie.",
        "poster_path": f"/poster{tmdb_id}.jpg",
        "backdrop_path": f"/bg{tmdb_id}.jpg",
        "release_date": "2026-09-15",
        "popularity": 80.0,
        "vote_average": 6.5,
        "genre_ids": [28, 12],
    }


def _make_discover_response(results, page=1, total_pages=1, total_results=None):
    return {
        "page": page,
        "results": results,
        "total_pages": total_pages,
        "total_results": total_results or len(results),
    }


@pytest.fixture
def mem_db():
    """In-memory SQLite with the tables browse_tmdb.py needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE show ("
        "  id TEXT PRIMARY KEY, status TEXT, tracked INTEGER DEFAULT 1"
        ")"
    )
    conn.execute(
        "CREATE TABLE show_external_id ("
        "  show_id TEXT, service TEXT, external_id TEXT,"
        "  UNIQUE(show_id, service)"
        ")"
    )
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear browse_tmdb cache before each test."""
    browse_tmdb._cache.clear()
    yield
    browse_tmdb._cache.clear()


@pytest.fixture
def mock_cfg():
    """Patch config to return a config with tmdb_api_key."""
    cfg = MagicMock()
    cfg.tmdb_api_key = "test-key"
    with patch.object(browse_tmdb.config, "get_current", return_value=cfg):
        yield cfg


# ── Cache tests ──────────────────────────────────────────


def test_cache_hit_avoids_tmdb_call(mem_db, mock_cfg):
    discover_data = _make_discover_response([_make_tmdb_tv_result(100)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data) as mock_discover:
        result1 = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9, 1)
        result2 = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9, 1)

    assert mock_discover.call_count == 1  # second call served from cache
    assert len(result1["items"]) == 1
    assert len(result2["items"]) == 1


def test_cache_miss_on_different_month(mem_db, mock_cfg):
    discover_data = _make_discover_response([_make_tmdb_tv_result(100)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data) as mock_discover:
        browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9, 1)
        browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 10, 1)

    assert mock_discover.call_count == 2


def test_cache_expires(mem_db, mock_cfg):
    discover_data = _make_discover_response([_make_tmdb_tv_result(100)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data) as mock_discover:
        browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9, 1)

        # Force expire
        key = ("tv", 2026, 9, 1)
        cached_at, data = browse_tmdb._cache[key]
        browse_tmdb._cache[key] = (cached_at - 100000, data)

        browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9, 1)

    assert mock_discover.call_count == 2


# ── Cross-reference tests ───────────────────────────────


def test_tracked_show_matched_via_tmdb_id(mem_db, mock_cfg):
    mem_db.execute("INSERT INTO show VALUES ('s-abc', 'WATCHING', 1)")
    mem_db.execute(
        "INSERT INTO show_external_id VALUES ('s-abc', 'tmdb', '12345')"
    )
    mem_db.commit()

    discover_data = _make_discover_response([_make_tmdb_tv_result(12345)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)

    item = result["items"][0]
    assert item["lcars_show_id"] == "s-abc"
    assert item["lcars_status"] == "WATCHING"


def test_untracked_stub_ignored(mem_db, mock_cfg):
    mem_db.execute("INSERT INTO show VALUES ('s-stub', 'PLANNED', 0)")  # tracked=0
    mem_db.execute(
        "INSERT INTO show_external_id VALUES ('s-stub', 'tmdb', '11111')"
    )
    mem_db.commit()

    discover_data = _make_discover_response([_make_tmdb_tv_result(11111)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)

    item = result["items"][0]
    assert item["lcars_show_id"] is None  # stub not matched


def test_no_match_returns_null_lcars_fields(mem_db, mock_cfg):
    discover_data = _make_discover_response([_make_tmdb_tv_result(99999)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)

    item = result["items"][0]
    assert item["lcars_show_id"] is None
    assert item["lcars_status"] is None


# ── Flattening tests ────────────────────────────────────


def test_flatten_tv_shape(mem_db, mock_cfg):
    discover_data = _make_discover_response([_make_tmdb_tv_result(42, "Reacher")])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)

    item = result["items"][0]
    assert item["tmdb_id"] == 42
    assert item["media_type"] == "TV"
    assert item["title"] == "Reacher"
    assert item["poster_url"] == "https://image.tmdb.org/t/p/w300/poster42.jpg"
    assert item["first_air_date"] == "2026-09-01"
    assert item["release_date"] is None


def test_flatten_movie_shape(mem_db, mock_cfg):
    discover_data = _make_discover_response([_make_tmdb_movie_result(77, "The Runner")])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_movies",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_movies(mem_db, 2026, 9)

    item = result["items"][0]
    assert item["tmdb_id"] == 77
    assert item["media_type"] == "MOVIE"
    assert item["title"] == "The Runner"
    assert item["release_date"] == "2026-09-15"
    assert item["first_air_date"] is None


def test_flatten_handles_missing_poster(mem_db, mock_cfg):
    tv = _make_tmdb_tv_result(1)
    tv["poster_path"] = None
    tv["backdrop_path"] = None
    discover_data = _make_discover_response([tv])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)

    item = result["items"][0]
    assert item["poster_url"] is None
    assert item["backdrop_url"] is None


# ── Result shape tests ──────────────────────────────────


def test_result_shape(mem_db, mock_cfg):
    discover_data = _make_discover_response(
        [_make_tmdb_tv_result(1), _make_tmdb_tv_result(2)],
        page=1, total_pages=5, total_results=100,
    )

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=discover_data):
        result = browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)

    assert result["current_page"] == 1
    assert result["total_pages"] == 5
    assert result["total_results"] == 100
    assert result["has_next_page"] is True
    assert len(result["items"]) == 2


# ── Combined browse tests ──────────────────────────────


def test_browse_all_merges_tv_and_movies(mem_db, mock_cfg):
    tv_data = _make_discover_response([
        _make_tmdb_tv_result(1, "Show A"),
    ])
    tv_data["results"][0]["popularity"] = 50.0

    movie_data = _make_discover_response([
        _make_tmdb_movie_result(2, "Movie B"),
    ])
    movie_data["results"][0]["popularity"] = 100.0

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=tv_data), \
         patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_movies",
                      return_value=movie_data):
        result = browse_tmdb.fetch_tmdb_browse(mem_db, 2026, 9, "ALL")

    assert len(result["items"]) == 2
    # Sorted by popularity: movie first
    assert result["items"][0]["title"] == "Movie B"
    assert result["items"][1]["title"] == "Show A"


def test_browse_tv_only(mem_db, mock_cfg):
    tv_data = _make_discover_response([_make_tmdb_tv_result(1)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_tv",
                      return_value=tv_data) as mock_tv:
        result = browse_tmdb.fetch_tmdb_browse(mem_db, 2026, 9, "TV")

    assert len(result["items"]) == 1
    assert result["items"][0]["media_type"] == "TV"
    mock_tv.assert_called_once()


def test_browse_movie_only(mem_db, mock_cfg):
    movie_data = _make_discover_response([_make_tmdb_movie_result(1)])

    with patch.object(browse_tmdb.tmdb_client.TmdbClient, "discover_movies",
                      return_value=movie_data) as mock_movies:
        result = browse_tmdb.fetch_tmdb_browse(mem_db, 2026, 9, "MOVIE")

    assert len(result["items"]) == 1
    assert result["items"][0]["media_type"] == "MOVIE"
    mock_movies.assert_called_once()


# ── Date range tests ────────────────────────────────────


def test_date_range_normal_month():
    gte, lte = browse_tmdb._date_range(2026, 9)
    assert gte == "2026-09-01"
    assert lte == "2026-09-30"


def test_date_range_february_leap():
    gte, lte = browse_tmdb._date_range(2028, 2)
    assert gte == "2028-02-01"
    assert lte == "2028-02-29"


def test_date_range_february_non_leap():
    gte, lte = browse_tmdb._date_range(2027, 2)
    assert gte == "2027-02-01"
    assert lte == "2027-02-28"


def test_date_range_december():
    gte, lte = browse_tmdb._date_range(2026, 12)
    assert gte == "2026-12-01"
    assert lte == "2026-12-31"


# ── Error handling ──────────────────────────────────────


def test_missing_api_key_raises(mem_db):
    cfg = MagicMock()
    cfg.tmdb_api_key = None
    with patch.object(browse_tmdb.config, "get_current", return_value=cfg):
        with pytest.raises(browse_tmdb.tmdb_client.TmdbError,
                           match="TMDB API key not configured"):
            browse_tmdb.fetch_tmdb_tv(mem_db, 2026, 9)
