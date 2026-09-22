"""Art-fetch negative cache + staged fetch (NEXT_UP.md "Art-fetch
negative cache + throttle"). Covers:
- update_art_negative_cache stamping/clearing show.poster_art_not_found_at
  / banner_art_not_found_at based on whether a selected show-level asset
  exists.
- fetch_show_art_for_seasons scoping to exactly the given season ids,
  never touching AniList for anything else.
- fetch_show_art_show_level's AniList-fallback-only-when-no-season-link
  behavior.
- fetch_show_art (the manual/full wrapper) still doing everything.
- anilist_client.manual_priority()/manual_request_pending().
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, art, config, metadata


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "art_staging_test.db"
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


@pytest.fixture(autouse=True)
def _reset_config():
    config.set_current(config.Config())
    yield
    config.set_current(config.Config())


def _show(conn, show_id, tracking_space="anime"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, 'Test Show', 'romaji', 'watching', 1, 'x', 'x')",
        (show_id, tracking_space),
    )
    conn.commit()


def _external_id(conn, show_id, service, external_id):
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, 'https://x', 'x')",
        (show_id, service, external_id),
    )
    conn.commit()


def _season(conn, season_id, show_id, season_number, anilist_id=None):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id,"
        " source, matched, manual_override, created_at, updated_at)"
        " VALUES (?, ?, ?, 'watching', ?, 'fribb', 0, 0, 'x', 'x')",
        (season_id, show_id, season_number, anilist_id),
    )
    conn.commit()


def _anilist_media(poster="https://cdn/p.jpg", banner="https://cdn/b.jpg"):
    return {"coverImage": {"large": poster, "extraLarge": None}, "bannerImage": banner}


class TestUpdateArtNegativeCache:
    def test_stamps_both_when_nothing_selected(self, conn):
        _show(conn, "s-neg001")
        metadata.update_art_negative_cache(conn, "s-neg001")
        row = conn.execute(
            "SELECT poster_art_not_found_at, banner_art_not_found_at FROM show WHERE id = ?",
            ("s-neg001",),
        ).fetchone()
        assert row["poster_art_not_found_at"] is not None
        assert row["banner_art_not_found_at"] is not None

    def test_clears_poster_when_show_level_selected_exists(self, conn):
        _show(conn, "s-neg002")
        aid = art.upsert_asset(conn, "s-neg002", None, "poster", "manual", "https://cdn/p.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        metadata.update_art_negative_cache(conn, "s-neg002")

        row = conn.execute(
            "SELECT poster_art_not_found_at, banner_art_not_found_at FROM show WHERE id = ?",
            ("s-neg002",),
        ).fetchone()
        assert row["poster_art_not_found_at"] is None
        assert row["banner_art_not_found_at"] is not None  # still no banner

    def test_season_level_selection_does_not_count(self, conn):
        """A selected asset scoped to a season (not season_id IS NULL)
        doesn't satisfy the show-level slot renderHero/renderBanner
        actually read from — negative cache should still stamp."""
        _show(conn, "s-neg003")
        _season(conn, "z-neg001", "s-neg003", 1)
        aid = art.upsert_asset(conn, "s-neg003", "z-neg001", "poster", "anilist",
                               "https://cdn/season-p.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        metadata.update_art_negative_cache(conn, "s-neg003")

        row = conn.execute(
            "SELECT poster_art_not_found_at FROM show WHERE id = ?", ("s-neg003",),
        ).fetchone()
        assert row["poster_art_not_found_at"] is not None

    def test_background_kind_satisfies_banner_slot(self, conn):
        _show(conn, "s-neg004")
        aid = art.upsert_asset(conn, "s-neg004", None, "background", "tmdb",
                               "https://cdn/bg.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        metadata.update_art_negative_cache(conn, "s-neg004")

        row = conn.execute(
            "SELECT banner_art_not_found_at FROM show WHERE id = ?", ("s-neg004",),
        ).fetchone()
        assert row["banner_art_not_found_at"] is None


class TestFetchShowArtForSeasons:
    def test_only_fetches_requested_seasons(self, conn, monkeypatch):
        _show(conn, "s-stg001")
        _season(conn, "z-stg001", "s-stg001", 1, anilist_id=100)
        _season(conn, "z-stg002", "s-stg001", 2, anilist_id=200)

        called_with = []

        def fake_fetch_media(al_id):
            called_with.append(al_id)
            return _anilist_media()

        monkeypatch.setattr(anilist_client, "fetch_media", fake_fetch_media)

        metadata.fetch_show_art_for_seasons(conn, "s-stg001", ["z-stg002"])

        assert called_with == [200]

    def test_empty_season_ids_is_a_noop(self, conn, monkeypatch):
        _show(conn, "s-stg002")
        called = []
        monkeypatch.setattr(anilist_client, "fetch_media",
                            lambda *a, **kw: called.append(1) or _anilist_media())

        result = metadata.fetch_show_art_for_seasons(conn, "s-stg002", [])

        assert result == 0
        assert called == []

    def test_stamps_negative_cache_after_running(self, conn, monkeypatch):
        _show(conn, "s-stg003")
        _season(conn, "z-stg003", "s-stg003", 1, anilist_id=300)
        monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: None)

        metadata.fetch_show_art_for_seasons(conn, "s-stg003", ["z-stg003"])

        row = conn.execute(
            "SELECT poster_art_not_found_at FROM show WHERE id = ?", ("s-stg003",),
        ).fetchone()
        assert row["poster_art_not_found_at"] is not None


class TestFetchShowArtShowLevel:
    def test_skips_anilist_fallback_when_a_season_has_a_link(self, conn, monkeypatch):
        _show(conn, "s-stg004")
        _season(conn, "z-stg004", "s-stg004", 1, anilist_id=400)
        called = []
        monkeypatch.setattr(anilist_client, "fetch_media",
                            lambda *a, **kw: called.append(1) or _anilist_media())

        metadata.fetch_show_art_show_level(conn, "s-stg004")

        assert called == []

    def test_uses_anilist_fallback_when_no_season_has_a_link(self, conn, monkeypatch):
        _show(conn, "s-stg005")
        _season(conn, "z-stg005", "s-stg005", 1, anilist_id=None)
        _external_id(conn, "s-stg005", "anilist", "500")
        monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: _anilist_media())

        metadata.fetch_show_art_show_level(conn, "s-stg005")

        row = conn.execute(
            "SELECT poster_url FROM show WHERE id = ?", ("s-stg005",),
        ).fetchone()
        assert row["poster_url"] == "https://cdn/p.jpg"


class TestFetchShowArtWrapper:
    def test_fetches_all_seasons_plus_show_level(self, conn, monkeypatch):
        _show(conn, "s-stg006")
        _season(conn, "z-stg006", "s-stg006", 1, anilist_id=600)
        _season(conn, "z-stg007", "s-stg006", 2, anilist_id=700)

        called_with = []

        def fake_fetch_media(al_id):
            called_with.append(al_id)
            return _anilist_media()

        monkeypatch.setattr(anilist_client, "fetch_media", fake_fetch_media)

        metadata.fetch_show_art(conn, "s-stg006")

        assert sorted(called_with) == [600, 700]


class TestManualPriority:
    def test_not_pending_by_default(self):
        assert anilist_client.manual_request_pending() is False

    def test_pending_inside_context_manager(self):
        with anilist_client.manual_priority():
            assert anilist_client.manual_request_pending() is True
        assert anilist_client.manual_request_pending() is False

    def test_nested_contexts_stay_pending_until_outermost_exits(self):
        with anilist_client.manual_priority():
            with anilist_client.manual_priority():
                pass
            assert anilist_client.manual_request_pending() is True
        assert anilist_client.manual_request_pending() is False
