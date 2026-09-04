"""Tests for the anime seasonal browse module (src/lcars/browse.py)."""

import sqlite3
from unittest.mock import patch

import pytest

from lcars import browse

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_anilist_page(media_list, page=1, total=None, has_next=False):
    """Build an AniList Page dict matching the seasonal query shape."""
    return {
        "pageInfo": {
            "total": total or len(media_list),
            "currentPage": page,
            "lastPage": 1,
            "hasNextPage": has_next,
        },
        "media": media_list,
    }


def _make_media(anilist_id, title_en="Test Show", title_ro=None, fmt="TV", mal_id=None):
    return {
        "id": anilist_id,
        "idMal": mal_id,
        "title": {"romaji": title_ro or title_en, "english": title_en, "native": None},
        "coverImage": {"large": f"https://img.example.com/{anilist_id}.jpg"},
        "description": "A test synopsis.",
        "genres": ["Action", "Fantasy"],
        "format": fmt,
        "episodes": 12,
        "duration": 24,
        "status": "NOT_YET_RELEASED",
        "startDate": {"year": 2026, "month": 10, "day": 1},
        "studios": {"nodes": [{"name": "Studio A"}]},
    }


@pytest.fixture
def mem_db():
    """In-memory SQLite with the tables browse.py needs."""
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
    conn.execute(
        "CREATE TABLE season ("
        "  id TEXT PRIMARY KEY, show_id TEXT, season_number INTEGER,"
        "  anilist_id INTEGER, status TEXT"
        ")"
    )
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear browse cache before each test."""
    browse._cache.clear()
    yield
    browse._cache.clear()


# ── Cache tests ──────────────────────────────────────────────────────


def test_cache_hit_avoids_anilist_call(mem_db):
    page_data = _make_anilist_page([_make_media(100)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data) as mock:
        result1 = browse.fetch_seasonal_browse(mem_db, "FALL", 2026, 1)
        result2 = browse.fetch_seasonal_browse(mem_db, "FALL", 2026, 1)

    assert mock.call_count == 1  # second call served from cache
    assert len(result1["items"]) == 1
    assert result2["items"] == result1["items"]


def test_cache_miss_on_different_season(mem_db):
    page_data = _make_anilist_page([_make_media(100)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data) as mock:
        browse.fetch_seasonal_browse(mem_db, "FALL", 2026, 1)
        browse.fetch_seasonal_browse(mem_db, "WINTER", 2027, 1)

    assert mock.call_count == 2


def test_cache_expires(mem_db):
    page_data = _make_anilist_page([_make_media(100)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data) as mock:
        browse.fetch_seasonal_browse(mem_db, "FALL", 2026, 1)

        # Force expire
        key = ("FALL", 2026, 1)
        cached_at, data = browse._cache[key]
        browse._cache[key] = (cached_at - 100000, data)  # way in the past

        browse.fetch_seasonal_browse(mem_db, "FALL", 2026, 1)

    assert mock.call_count == 2


# ── Cross-reference tests ───────────────────────────────────────────


def test_tracked_show_matched_via_show_external_id(mem_db):
    mem_db.execute("INSERT INTO show VALUES ('s-abc', 'WATCHING', 1)")
    mem_db.execute(
        "INSERT INTO show_external_id VALUES ('s-abc', 'anilist', '12345')"
    )
    mem_db.commit()

    page_data = _make_anilist_page([_make_media(12345)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data):
        result = browse.fetch_seasonal_browse(mem_db, "FALL", 2026)

    item = result["items"][0]
    assert item["lcars_show_id"] == "s-abc"
    assert item["lcars_status"] == "WATCHING"


def test_tracked_show_matched_via_season_anilist_id(mem_db):
    mem_db.execute("INSERT INTO show VALUES ('s-def', 'PLANNED', 1)")
    mem_db.execute(
        "INSERT INTO season VALUES ('z-s1', 's-def', 1, 67890, 'WATCHING')"
    )
    mem_db.commit()

    page_data = _make_anilist_page([_make_media(67890)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data):
        result = browse.fetch_seasonal_browse(mem_db, "FALL", 2026)

    item = result["items"][0]
    assert item["lcars_show_id"] == "s-def"
    assert item["lcars_status"] == "PLANNED"
    assert item["lcars_season_id"] == "z-s1"
    assert item["lcars_season_status"] == "WATCHING"


def test_untracked_stub_ignored(mem_db):
    mem_db.execute("INSERT INTO show VALUES ('s-stub', 'PLANNED', 0)")  # tracked=0
    mem_db.execute(
        "INSERT INTO show_external_id VALUES ('s-stub', 'anilist', '11111')"
    )
    mem_db.commit()

    page_data = _make_anilist_page([_make_media(11111)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data):
        result = browse.fetch_seasonal_browse(mem_db, "FALL", 2026)

    item = result["items"][0]
    assert item["lcars_show_id"] is None  # stub not matched


def test_no_match_returns_null_lcars_fields(mem_db):
    page_data = _make_anilist_page([_make_media(99999)])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data):
        result = browse.fetch_seasonal_browse(mem_db, "FALL", 2026)

    item = result["items"][0]
    assert item["lcars_show_id"] is None
    assert item["lcars_status"] is None
    assert item["lcars_season_id"] is None


# ── Flattening tests ────────────────────────────────────────────────


def test_flatten_handles_null_fields(mem_db):
    media = {
        "id": 1,
        "idMal": None,
        "title": {"romaji": "Test", "english": None, "native": None},
        "coverImage": None,
        "description": None,
        "genres": None,
        "format": None,
        "episodes": None,
        "duration": None,
        "status": None,
        "startDate": None,
        "studios": None,
    }
    page_data = _make_anilist_page([media])

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data):
        result = browse.fetch_seasonal_browse(mem_db, "FALL", 2026)

    item = result["items"][0]
    assert item["cover_image_url"] is None
    assert item["genres"] == []
    assert item["studio_names"] == []
    assert item["start_date"] is None


def test_fuzzy_date_formatting(mem_db):
    media = _make_media(1)
    # Full date
    media["startDate"] = {"year": 2026, "month": 10, "day": 5}
    assert browse._format_fuzzy_date(media["startDate"]) == "2026-10-05"

    # Year+month only
    media["startDate"] = {"year": 2026, "month": 10, "day": None}
    assert browse._format_fuzzy_date(media["startDate"]) == "2026-10"

    # Year only
    media["startDate"] = {"year": 2026, "month": None, "day": None}
    assert browse._format_fuzzy_date(media["startDate"]) == "2026"

    # Null
    assert browse._format_fuzzy_date(None) is None
    assert browse._format_fuzzy_date({}) is None


def test_result_shape(mem_db):
    page_data = _make_anilist_page(
        [_make_media(1), _make_media(2)], page=1, total=50, has_next=True
    )

    with patch.object(browse.anilist_client, "fetch_seasonal_page", return_value=page_data):
        result = browse.fetch_seasonal_browse(mem_db, "FALL", 2026)

    assert result["current_page"] == 1
    assert result["total"] == 50
    assert result["has_next_page"] is True
    assert len(result["items"]) == 2


def test_invalid_season_raises(mem_db):
    with pytest.raises(ValueError, match="Invalid season"):
        browse.fetch_seasonal_browse(mem_db, "AUTUMN", 2026)
