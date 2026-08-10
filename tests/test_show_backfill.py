"""One-time/repeatable show backfill — SCOPE.md §5.1/§5.2, BUILD_PLAN.md
B.11d. Same real-migrated-SQLite-DB + fake-client approach
test_local_audit.py already established (show_backfill.py reuses
local_audit.audit_local_files() directly for its own untracked-show
computation).
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, show_backfill, sonarr_client


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "show_backfill_test.db"
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


def _configure_sonarr():
    cfg = config.get_current()
    cfg.sonarr_url = "http://sonarr.test"
    cfg.sonarr_api_key = "test-key"


class _FakeSonarrClient:
    def __init__(self, series_list):
        self._series_list = series_list

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_series(self):
        return self._series_list

    def episodes(self, series_id, include_episode_file=False):
        return []  # metadata.py's own _fetch_sonarr — no episodes needed for these tests


def _sonarr_series(series_id, tvdb_id, title, series_type=None):
    return {"id": series_id, "tvdbId": tvdb_id, "title": title, "seriesType": series_type}


# --- classification (pure) ---------------------------------------------------


def test_classify_sonarr_anime_flag_maps_to_anime_tracking_space():
    entry = {"service": "sonarr", "title": "Show A", "external_id": 1, "series_type": "anime"}
    result = show_backfill._classify(entry)
    assert result["tracking_space"] == "anime"
    assert result["media_shape"] == "episodic"
    assert result["tvdb_id"] == 1


def test_classify_sonarr_non_anime_maps_to_tv_tracking_space():
    entry = {"service": "sonarr", "title": "Show B", "external_id": 2, "series_type": "standard"}
    assert show_backfill._classify(entry)["tracking_space"] == "tv"


def test_classify_sonarr_missing_series_type_defaults_to_tv():
    entry = {"service": "sonarr", "title": "Show C", "external_id": 3}
    assert show_backfill._classify(entry)["tracking_space"] == "tv"


def test_classify_radarr_always_defaults_to_tv():
    # No anime signal exists from Radarr at all — documented cut, B.11d.
    entry = {"service": "radarr", "title": "Movie A", "external_id": 10}
    result = show_backfill._classify(entry)
    assert result["tracking_space"] == "tv"
    assert result["media_shape"] == "movie"
    assert result["tmdb_id"] == 10


# --- preview_backfill (dry-run) -----------------------------------------------


def test_preview_backfill_lists_untracked_items_without_writing(conn, monkeypatch):
    _configure_sonarr()
    series = [_sonarr_series(1, 111, "Untracked Show", series_type="anime")]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))

    preview = show_backfill.preview_backfill(conn)
    assert preview == [
        {
            "service": "sonarr",
            "title": "Untracked Show",
            "external_id": 111,
            "tracking_space": "anime",
            "media_shape": "episodic",
        }
    ]
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0


def test_preview_backfill_is_empty_with_nothing_untracked(conn, monkeypatch):
    _configure_sonarr()
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient([]))
    assert show_backfill.preview_backfill(conn) == []


# --- backfill_untracked_shows (the real run) ----------------------------------


def test_backfill_creates_a_show_per_untracked_item(conn, monkeypatch):
    _configure_sonarr()
    series = [
        _sonarr_series(1, 111, "Show A", series_type="standard"),
        _sonarr_series(2, 222, "Show B", series_type="anime"),
    ]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))
    monkeypatch.setattr(show_backfill, "ANIME_ADD_THROTTLE_SECONDS", 0)  # no real sleep in tests

    result = show_backfill.backfill_untracked_shows(conn)
    assert len(result["created"]) == 2
    assert result["failed"] == []

    rows = conn.execute("SELECT title_romaji, tracking_space, status FROM show").fetchall()
    by_title = {r["title_romaji"]: r for r in rows}
    assert by_title["Show A"]["tracking_space"] == "tv"
    assert by_title["Show B"]["tracking_space"] == "anime"
    # No AniList token configured — status seed no-ops, stays at the
    # addShow default.
    assert by_title["Show A"]["status"] == "planned"
    assert by_title["Show B"]["status"] == "planned"


def test_backfill_is_idempotent_on_rerun(conn, monkeypatch):
    _configure_sonarr()
    series = [_sonarr_series(1, 111, "Show A", series_type="standard")]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))
    monkeypatch.setattr(show_backfill, "ANIME_ADD_THROTTLE_SECONDS", 0)

    first = show_backfill.backfill_untracked_shows(conn)
    assert len(first["created"]) == 1

    second = show_backfill.backfill_untracked_shows(conn)
    # already tracked now — local_audit's own known-id lookup excludes it
    assert second["created"] == []
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 1


def test_backfill_throttles_only_between_anime_adds(conn, monkeypatch):
    _configure_sonarr()
    series = [
        _sonarr_series(1, 111, "TV Show", series_type="standard"),
        _sonarr_series(2, 222, "Anime Show A", series_type="anime"),
        _sonarr_series(3, 333, "Anime Show B", series_type="anime"),
    ]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))
    sleeps = []
    monkeypatch.setattr(show_backfill.time, "sleep", lambda s: sleeps.append(s))

    show_backfill.backfill_untracked_shows(conn)
    assert len(sleeps) == 2  # once per anime show, not the tv one
    assert sleeps == [show_backfill.ANIME_ADD_THROTTLE_SECONDS] * 2


# --- _seed_status_from_anilist -------------------------------------------------


def _bare_anime_show(conn, show_id, anilist_id=None):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', 'Test Anime', 'romaji', 'planned', 1, 'x', 'x')",
        (show_id,),
    )
    if anilist_id is not None:
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'anilist', ?, 'https://x', 'x')",
            (show_id, str(anilist_id)),
        )
    conn.commit()


def test_seed_status_updates_status_and_writes_history(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    _bare_anime_show(conn, "s-seed01", anilist_id=999)
    monkeypatch.setattr(
        anilist_client, "fetch_my_list_status", lambda token, aid, **kw: "COMPLETED"
    )

    show_backfill._seed_status_from_anilist(conn, "s-seed01")

    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed01",)).fetchone()
    assert row["status"] == "completed"
    change = conn.execute(
        "SELECT previous_status, new_status, changed_by FROM status_change WHERE show_id = ?",
        ("s-seed01",),
    ).fetchone()
    assert change["previous_status"] == "planned"
    assert change["new_status"] == "completed"
    assert change["changed_by"] == "show_backfill"


def test_seed_status_repeating_maps_to_watching(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    _bare_anime_show(conn, "s-seed02", anilist_id=999)
    monkeypatch.setattr(
        anilist_client, "fetch_my_list_status", lambda token, aid, **kw: "REPEATING"
    )
    show_backfill._seed_status_from_anilist(conn, "s-seed02")
    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed02",)).fetchone()
    assert row["status"] == "watching"


def test_seed_status_no_token_is_a_clean_no_op(conn):
    _bare_anime_show(conn, "s-seed03", anilist_id=999)
    show_backfill._seed_status_from_anilist(conn, "s-seed03")
    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed03",)).fetchone()
    assert row["status"] == "planned"


def test_seed_status_no_anilist_link_is_a_clean_no_op(conn):
    config.get_current().anilist_access_token = "tok"
    _bare_anime_show(conn, "s-seed04", anilist_id=None)
    show_backfill._seed_status_from_anilist(conn, "s-seed04")
    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed04",)).fetchone()
    assert row["status"] == "planned"


def test_seed_status_no_viewer_list_entry_is_a_clean_no_op(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    _bare_anime_show(conn, "s-seed05", anilist_id=999)
    monkeypatch.setattr(anilist_client, "fetch_my_list_status", lambda token, aid, **kw: None)
    show_backfill._seed_status_from_anilist(conn, "s-seed05")
    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed05",)).fetchone()
    assert row["status"] == "planned"


def test_seed_status_anilist_error_is_a_clean_no_op(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    _bare_anime_show(conn, "s-seed06", anilist_id=999)

    def _raise(token, aid, **kw):
        raise anilist_client.AniListError("boom")

    monkeypatch.setattr(anilist_client, "fetch_my_list_status", _raise)
    show_backfill._seed_status_from_anilist(conn, "s-seed06")  # should not raise
    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed06",)).fetchone()
    assert row["status"] == "planned"
