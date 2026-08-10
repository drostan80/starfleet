"""Local file availability polling — SCOPE.md §5.2/§6.7, BUILD_PLAN.md
B.3. Exercised against a real, migrated SQLite database (generated
columns, real FK/CHECK constraints — same reasoning test_export_import.py
already uses for exactly this) with fake Sonarr/Radarr clients (no real
network calls) shaped after the real, live API responses this module
was verified against before being built.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import availability, config, radarr_client, service_health, sonarr_client, util


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "availability_test.db"
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


def _configure_radarr():
    cfg = config.get_current()
    cfg.radarr_url = "http://radarr.test"
    cfg.radarr_api_key = "test-key"


def _add_show(conn, show_id, tvdb_id=None, tmdb_id=None, media_shape="episodic"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, ?, 'tv', 'Test Show', 'romaji', 'watching', 1, 'x', 'x')",
        (show_id, media_shape),
    )
    if tvdb_id is not None:
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tvdb', ?, 'https://x', 'x')",
            (show_id, str(tvdb_id)),
        )
    if tmdb_id is not None:
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tmdb', ?, 'https://x', 'x')",
            (show_id, str(tmdb_id)),
        )
    conn.commit()


def _add_episode(conn, episode_id, show_id, season=1, episode=1, air_date_utc=None):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, state,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, 'unwatched', 'x', 'x')",
        (episode_id, show_id, season, episode, air_date_utc),
    )
    conn.commit()


class _FakeHistoryClient:
    """Mimics the real /history endpoint's own pagination shape (newest
    first, page/pageSize/totalRecords) faithfully enough to exercise
    availability.py's own paging/checkpoint-stopping logic for real, not
    just hand it pre-split pages."""

    def __init__(self, records):
        self._records = records
        self.calls: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def history_page(self, page, page_size=250):
        self.calls.append(page)
        start = (page - 1) * page_size
        end = start + page_size
        return {"records": self._records[start:end], "totalRecords": len(self._records)}


def _sonarr_record(event_type, date, tvdb_id=457078, season=1, episode=1, imported_path=None):
    return {
        "date": date,
        "eventType": event_type,
        "series": {"id": 955, "tvdbId": tvdb_id},
        "episode": {"id": 36997, "seasonNumber": season, "episodeNumber": episode},
        "data": {"importedPath": imported_path} if imported_path else {},
    }


def _radarr_record(event_type, date, tmdb_id=687163, imported_path=None):
    return {
        "date": date,
        "eventType": event_type,
        "movie": {"id": 308, "tmdbId": tmdb_id},
        "data": {"importedPath": imported_path} if imported_path else {},
    }


# --- Sonarr -------------------------------------------------------------


def test_poll_sonarr_grabbed_event_sets_downloading(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl001", tvdb_id=457078)
    _add_episode(conn, "e-avl001", "s-avl001")
    fake = _FakeHistoryClient([_sonarr_record("grabbed", "2026-08-09T10:00:00Z")])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 1
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-avl001'"
    ).fetchone()
    assert row["available_via_sonarr"] == "downloading"
    assert row["file_path_sonarr"] is None


def test_poll_sonarr_imported_event_sets_available_with_path(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl002", tvdb_id=457078)
    _add_episode(conn, "e-avl002", "s-avl002")
    fake = _FakeHistoryClient(
        [
            _sonarr_record(
                "downloadFolderImported", "2026-08-09T10:05:00Z", imported_path="/data/x.mkv"
            )
        ]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 1
    row = conn.execute(
        "SELECT available_via_sonarr, available_locally, file_path_sonarr"
        " FROM episode WHERE id = 'e-avl002'"
    ).fetchone()
    assert row["available_via_sonarr"] == "available"
    assert row["available_locally"] == 1
    assert row["file_path_sonarr"] == "/data/x.mkv"


def test_poll_sonarr_deleted_event_sets_unavailable_and_clears_path(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl003", tvdb_id=457078)
    _add_episode(conn, "e-avl003", "s-avl003")
    conn.execute(
        "UPDATE episode SET available_via_sonarr = 'available', file_path_sonarr = '/data/old.mkv'"
        " WHERE id = 'e-avl003'"
    )
    conn.commit()
    fake = _FakeHistoryClient([_sonarr_record("episodeFileDeleted", "2026-08-09T10:10:00Z")])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    availability._poll_sonarr(conn, backfill=True)
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-avl003'"
    ).fetchone()
    assert row["available_via_sonarr"] == "unavailable"
    assert row["file_path_sonarr"] is None


def test_poll_sonarr_download_ignored_is_a_no_op(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl004", tvdb_id=457078)
    _add_episode(conn, "e-avl004", "s-avl004")
    fake = _FakeHistoryClient([_sonarr_record("downloadIgnored", "2026-08-09T10:15:00Z")])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 0
    row = conn.execute("SELECT available_via_sonarr FROM episode WHERE id = 'e-avl004'").fetchone()
    assert row["available_via_sonarr"] == "unavailable"  # untouched, still the default


def test_poll_sonarr_skips_an_untracked_show(conn, monkeypatch):
    _configure_sonarr()
    # No show linked to this tvdb id at all.
    fake = _FakeHistoryClient(
        [_sonarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", tvdb_id=999999)]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 0


def test_poll_sonarr_skips_an_unfetched_episode(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl005", tvdb_id=457078)
    # Show is tracked and linked, but this specific episode was never fetched (A.8's job).
    fake = _FakeHistoryClient(
        [_sonarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", season=5, episode=99)]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 0


def test_poll_sonarr_not_configured_is_a_clean_no_op(conn, monkeypatch):
    # _configure_sonarr() deliberately not called.
    called = []
    monkeypatch.setattr(
        sonarr_client, "SonarrClient", lambda *a, **kw: called.append(1) or _FakeHistoryClient([])
    )
    count = availability._poll_sonarr(conn)
    assert count == 0
    assert called == []  # never even constructed


def test_poll_sonarr_client_error_is_caught(conn, monkeypatch):
    _configure_sonarr()

    class _BrokenClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def history_page(self, page, page_size=250):
            raise sonarr_client.SonarrError("boom")

    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _BrokenClient())
    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 0  # caught, not raised
    health = next(r for r in service_health.get_all(conn) if r["service"] == "sonarr")
    assert health["status"] == "unreachable"
    assert "boom" in health["last_error_message"]


def test_poll_sonarr_success_records_ok_service_health_even_with_nothing_new(conn, monkeypatch):
    """§6.7, B.6 — a poll that succeeds but finds no new events must
    still record a health success; this used to return before any
    commit happened at all on that path."""
    _configure_sonarr()
    fake = _FakeHistoryClient([])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    availability._poll_sonarr(conn, backfill=True)
    health = next(r for r in service_health.get_all(conn) if r["service"] == "sonarr")
    assert health["status"] == "ok"


def test_poll_radarr_client_error_records_unreachable_service_health(conn, monkeypatch):
    _configure_radarr()

    class _BrokenClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def history_page(self, page, page_size=250):
            raise radarr_client.RadarrError("boom")

    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: _BrokenClient())
    count = availability._poll_radarr(conn, backfill=True)
    assert count == 0
    health = next(r for r in service_health.get_all(conn) if r["service"] == "radarr")
    assert health["status"] == "unreachable"
    assert "boom" in health["last_error_message"]


def test_poll_sonarr_checkpoint_advances_and_second_poll_only_sees_new_events(conn, monkeypatch):
    """The realistic lifecycle: an explicit backfill (user-triggered,
    `backfill=True`) establishes the checkpoint from real history, then
    Ops's own automatic, non-backfill polls only ever see what's new
    since it."""
    _configure_sonarr()
    _add_show(conn, "s-avl006", tvdb_id=457078)
    _add_episode(conn, "e-avl006", "s-avl006")

    fake1 = _FakeHistoryClient([_sonarr_record("grabbed", "2026-08-09T10:00:00Z")])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake1)
    availability._poll_sonarr(conn, backfill=True)
    checkpoint = conn.execute(
        "SELECT last_event_at FROM availability_poll_checkpoint WHERE service = 'sonarr'"
    ).fetchone()
    assert checkpoint["last_event_at"] == "2026-08-09T10:00:00Z"

    # Second poll: the same old event is still in Sonarr's history (newest-first
    # includes everything), plus one genuinely new one — only the new one applies.
    fake2 = _FakeHistoryClient(
        [
            _sonarr_record(
                "downloadFolderImported", "2026-08-09T11:00:00Z", imported_path="/data/new.mkv"
            ),
            _sonarr_record("grabbed", "2026-08-09T10:00:00Z"),
        ]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake2)
    count = availability._poll_sonarr(conn)
    assert count == 1
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-avl006'"
    ).fetchone()
    assert row["available_via_sonarr"] == "available"
    assert row["file_path_sonarr"] == "/data/new.mkv"


def test_poll_sonarr_processes_events_chronologically_within_one_poll(conn, monkeypatch):
    # A quality-upgrade cycle: delete then reimport. The real API returns
    # these newest-first — applying them in that raw order would leave the
    # episode incorrectly "unavailable" (the delete, applied last). Reversed
    # to chronological order, the reimport (genuinely later) wins.
    _configure_sonarr()
    _add_show(conn, "s-avl007", tvdb_id=457078)
    _add_episode(conn, "e-avl007", "s-avl007")
    fake = _FakeHistoryClient(
        [
            _sonarr_record(
                "downloadFolderImported", "2026-08-09T12:00:00Z", imported_path="/data/better.mkv"
            ),
            _sonarr_record("episodeFileDeleted", "2026-08-09T11:59:00Z"),
        ]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    availability._poll_sonarr(conn, backfill=True)
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-avl007'"
    ).fetchone()
    assert row["available_via_sonarr"] == "available"
    assert row["file_path_sonarr"] == "/data/better.mkv"


# --- first-call seed-and-skip / manual backfill (2026-08-09, added after B.3's
# own build once the blocking cost of a never-polled service's first automatic
# poll was worked through with the user — see availability.py's module
# docstring) ------------------------------------------------------------------


def test_poll_sonarr_first_ever_call_seeds_checkpoint_without_processing_history(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl008", tvdb_id=457078)
    _add_episode(conn, "e-avl008", "s-avl008")
    fake = _FakeHistoryClient(
        [_sonarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/x.mkv")]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    monkeypatch.setattr(util, "now_utc_iso", lambda: "2030-01-01T00:00:00Z")

    count = availability._poll_sonarr(conn)  # backfill=False, the automatic default
    assert count == 0
    assert fake.calls == []  # never even paged through history
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-avl008'"
    ).fetchone()
    assert row["available_via_sonarr"] == "unavailable"  # untouched, still the default
    checkpoint = conn.execute(
        "SELECT last_event_at FROM availability_poll_checkpoint WHERE service = 'sonarr'"
    ).fetchone()
    assert checkpoint["last_event_at"] == "2030-01-01T00:00:00Z"  # seeded to "now", not the event


def test_poll_radarr_first_ever_call_seeds_checkpoint_without_processing_history(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-avl108", tmdb_id=687163, media_shape="movie")
    fake = _FakeHistoryClient(
        [_radarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/m.mkv")]
    )
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)
    monkeypatch.setattr(util, "now_utc_iso", lambda: "2030-01-01T00:00:00Z")

    count = availability._poll_radarr(conn)  # backfill=False, the automatic default
    assert count == 0
    assert fake.calls == []
    checkpoint = conn.execute(
        "SELECT last_event_at FROM availability_poll_checkpoint WHERE service = 'radarr'"
    ).fetchone()
    assert checkpoint["last_event_at"] == "2030-01-01T00:00:00Z"


def test_backfill_sonarr_ignores_an_existing_checkpoint_and_walks_full_history(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-avl009", tvdb_id=457078)
    _add_episode(conn, "e-avl009", "s-avl009")
    # A checkpoint already exists, well after the fake record's own date — an
    # automatic (non-backfill) poll would see nothing new at all.
    conn.execute(
        "INSERT INTO availability_poll_checkpoint (service, last_event_at, updated_at)"
        " VALUES ('sonarr', '2026-08-09T23:00:00Z', 'x')"
    )
    conn.commit()
    fake = _FakeHistoryClient(
        [_sonarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/x.mkv")]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    count = availability._poll_sonarr(conn, backfill=True)
    assert count == 1  # the pre-checkpoint record was still processed
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-avl009'"
    ).fetchone()
    assert row["available_via_sonarr"] == "available"
    assert row["file_path_sonarr"] == "/x.mkv"
    checkpoint = conn.execute(
        "SELECT last_event_at FROM availability_poll_checkpoint WHERE service = 'sonarr'"
    ).fetchone()
    # Advances to the record actually walked, not left at the pre-existing value.
    assert checkpoint["last_event_at"] == "2026-08-09T10:00:00Z"


def test_backfill_file_availability_calls_both_services_in_backfill_mode(conn, monkeypatch):
    _configure_sonarr()
    _configure_radarr()
    _add_show(conn, "s-avl010", tvdb_id=457078)
    _add_episode(conn, "e-avl010", "s-avl010")
    _add_show(conn, "s-avl110", tmdb_id=687163, media_shape="movie")
    sonarr_fake = _FakeHistoryClient(
        [_sonarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/e.mkv")]
    )
    radarr_fake = _FakeHistoryClient(
        [_radarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/m.mkv")]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: sonarr_fake)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: radarr_fake)

    result = availability.backfill_file_availability(conn)
    assert result == {"episodes_updated": 1, "shows_updated": 1}


# --- Radarr ---------------------------------------------------------------


def test_poll_radarr_imported_event_sets_available_with_path(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-avl101", tmdb_id=687163, media_shape="movie")
    fake = _FakeHistoryClient(
        [_radarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/m.mkv")]
    )
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    count = availability._poll_radarr(conn, backfill=True)
    assert count == 1
    row = conn.execute(
        "SELECT available_via_radarr, available_locally, file_path_radarr"
        " FROM show WHERE id = 's-avl101'"
    ).fetchone()
    assert row["available_via_radarr"] == "available"
    assert row["available_locally"] == 1
    assert row["file_path_radarr"] == "/m.mkv"


def test_poll_radarr_deleted_event_sets_unavailable(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-avl102", tmdb_id=687163, media_shape="movie")
    conn.execute(
        "UPDATE show SET available_via_radarr = 'available', file_path_radarr = '/old.mkv'"
        " WHERE id = 's-avl102'"
    )
    conn.commit()
    fake = _FakeHistoryClient([_radarr_record("movieFileDeleted", "2026-08-09T10:00:00Z")])
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    availability._poll_radarr(conn, backfill=True)
    row = conn.execute(
        "SELECT available_via_radarr, file_path_radarr FROM show WHERE id = 's-avl102'"
    ).fetchone()
    assert row["available_via_radarr"] == "unavailable"
    assert row["file_path_radarr"] is None


def test_poll_radarr_skips_a_non_movie_show(conn, monkeypatch):
    _configure_radarr()
    # Same tmdb id, but an episodic show — Radarr events never touch episodic shows.
    _add_show(conn, "s-avl103", tmdb_id=687163, media_shape="episodic")
    fake = _FakeHistoryClient([_radarr_record("downloadFolderImported", "2026-08-09T10:00:00Z")])
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    count = availability._poll_radarr(conn, backfill=True)
    assert count == 0


def test_backfill_radarr_ignores_an_existing_checkpoint_and_walks_full_history(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-avl104", tmdb_id=687163, media_shape="movie")
    conn.execute(
        "INSERT INTO availability_poll_checkpoint (service, last_event_at, updated_at)"
        " VALUES ('radarr', '2026-08-09T23:00:00Z', 'x')"
    )
    conn.commit()
    fake = _FakeHistoryClient(
        [_radarr_record("downloadFolderImported", "2026-08-09T10:00:00Z", imported_path="/m.mkv")]
    )
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    count = availability._poll_radarr(conn, backfill=True)
    assert count == 1
    checkpoint = conn.execute(
        "SELECT last_event_at FROM availability_poll_checkpoint WHERE service = 'radarr'"
    ).fetchone()
    assert checkpoint["last_event_at"] == "2026-08-09T10:00:00Z"


def test_poll_radarr_not_configured_is_a_clean_no_op(conn, monkeypatch):
    called = []
    monkeypatch.setattr(
        radarr_client, "RadarrClient", lambda *a, **kw: called.append(1) or _FakeHistoryClient([])
    )
    count = availability._poll_radarr(conn)
    assert count == 0
    assert called == []


# --- recommended_poll_interval_seconds -------------------------------------


def test_recommended_interval_baseline_with_nothing_outstanding(conn):
    assert availability.recommended_poll_interval_seconds(conn) == 3600


def test_recommended_interval_urgent_for_a_just_aired_watching_episode(conn):
    _add_show(conn, "s-avl201")
    _add_episode(conn, "e-avl201", "s-avl201", air_date_utc="2026-08-09T09:00:00Z")
    # conftest-free: "now" in the test env is whatever real time it is when run;
    # anchor relative to a fixed recent-past date instead via direct SQL so this
    # isn't flaky — use the util helper the module itself uses for "now".
    conn.execute(
        "UPDATE episode SET air_date_utc = ? WHERE id = 'e-avl201'",
        (util.utc_iso_offset_hours(-1),),  # aired 1 hour ago — inside the urgent window
    )
    conn.commit()
    assert availability.recommended_poll_interval_seconds(conn) == 300


def test_recommended_interval_cooldown_for_an_older_unavailable_episode(conn):
    _add_show(conn, "s-avl202")
    _add_episode(
        conn, "e-avl202", "s-avl202", air_date_utc=util.utc_iso_offset_hours(-5)
    )  # aired 5h ago, past the 2h urgent window
    assert availability.recommended_poll_interval_seconds(conn) == 900


def test_recommended_interval_ignores_already_available_episodes(conn):
    _add_show(conn, "s-avl203")
    _add_episode(conn, "e-avl203", "s-avl203", air_date_utc=util.utc_iso_offset_hours(-1))
    conn.execute("UPDATE episode SET available_via_sonarr = 'available' WHERE id = 'e-avl203'")
    conn.commit()
    assert availability.recommended_poll_interval_seconds(conn) == 3600


def test_recommended_interval_ignores_non_watching_shows(conn):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-avl204', 'episodic', 'tv', 'Test Show', 'romaji', 'planned', 1, 'x', 'x')"
    )
    conn.commit()
    _add_episode(conn, "e-avl204", "s-avl204", air_date_utc=util.utc_iso_offset_hours(-1))
    assert availability.recommended_poll_interval_seconds(conn) == 3600


def test_recommended_interval_ignores_a_future_episode(conn):
    _add_show(conn, "s-avl205")
    _add_episode(conn, "e-avl205", "s-avl205", air_date_utc=util.utc_iso_offset_hours(1))
    assert availability.recommended_poll_interval_seconds(conn) == 3600
