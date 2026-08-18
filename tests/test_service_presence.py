"""show_service_presence automatic refresh — SCOPE.md §5.4/§6.7,
BUILD_PLAN.md B.7. Same real-migrated-SQLite-DB + fake-client approach
test_availability.py/test_local_audit.py already established.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import config, radarr_client, service_health, service_presence, sonarr_client


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "service_presence_test.db"
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


def _add_show(
    conn,
    show_id,
    title_romaji="Test Show",
    media_shape="episodic",
    tracked=True,
    available_via_radarr="unavailable",
):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, available_via_radarr, created_at, updated_at)"
        " VALUES (?, ?, 'tv', ?, 'romaji', 'watching', ?, ?, 'x', 'x')",
        (show_id, media_shape, title_romaji, int(tracked), available_via_radarr),
    )
    conn.commit()


def _add_episode(
    conn, episode_id, show_id, season=1, episode=1, available_via_sonarr="unavailable"
):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state,"
        " available_via_sonarr, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', 'unwatched', ?, 'x', 'x')",
        (episode_id, show_id, season, episode, available_via_sonarr),
    )
    conn.commit()


def _presence(conn, show_id, service):
    return conn.execute(
        "SELECT * FROM show_service_presence WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()


def _external_id(conn, show_id, service):
    return conn.execute(
        "SELECT * FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()


# --- refresh_local_presence ---------------------------------------------------


def test_local_presence_true_for_an_episodic_show_with_a_locally_available_episode(conn):
    _add_show(conn, "s-svp001")
    _add_episode(conn, "e-svp001", "s-svp001", available_via_sonarr="available")
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 1
    row = _presence(conn, "s-svp001", "local")
    assert row["present"] == 1


def test_local_presence_false_for_an_episodic_show_with_no_available_episode(conn):
    _add_show(conn, "s-svp002")
    _add_episode(conn, "e-svp002", "s-svp002", available_via_sonarr="downloading")
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 1  # new row created — false is still a real, first-seen value
    row = _presence(conn, "s-svp002", "local")
    assert row["present"] == 0


def test_local_presence_reflects_the_movie_shows_own_generated_column(conn):
    _add_show(conn, "s-svp003", media_shape="movie", available_via_radarr="available")
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 1
    row = _presence(conn, "s-svp003", "local")
    assert row["present"] == 1


def test_local_presence_skips_an_untracked_show(conn):
    _add_show(conn, "s-svp004", tracked=False)
    _add_episode(conn, "e-svp004", "s-svp004", available_via_sonarr="available")
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 0
    assert _presence(conn, "s-svp004", "local") is None


def test_local_presence_a_repeat_unchanged_call_is_not_counted_and_does_not_write(
    conn, monkeypatch
):
    """Caught in review before commit: this rollup runs every hour
    forever over every tracked show, so an unconditional write would
    mean a steady-state stream of pointless UPDATEs. A stable value
    must produce zero writes on the second pass, not just an
    uncounted one — checked_at itself must stay untouched too. Clock
    monkeypatched to a distinguishable value on the second call, since
    now_utc_iso()'s own second-level precision could otherwise hide a
    real write happening within the same wall-clock second."""
    _add_show(conn, "s-svp005")
    _add_episode(conn, "e-svp005", "s-svp005", available_via_sonarr="available")
    monkeypatch.setattr(service_presence.util, "now_utc_iso", lambda: "2020-01-01T00:00:00Z")
    service_presence.refresh_local_presence(conn)
    monkeypatch.setattr(service_presence.util, "now_utc_iso", lambda: "2030-01-01T00:00:00Z")
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 0
    assert _presence(conn, "s-svp005", "local")["checked_at"] == "2020-01-01T00:00:00Z"


def test_local_presence_a_real_flip_is_counted(conn):
    _add_show(conn, "s-svp006")
    _add_episode(conn, "e-svp006", "s-svp006", available_via_sonarr="unavailable")
    service_presence.refresh_local_presence(conn)
    conn.execute("UPDATE episode SET available_via_sonarr = 'available' WHERE id = 'e-svp006'")
    conn.commit()
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 1
    assert _presence(conn, "s-svp006", "local")["present"] == 1


def test_local_presence_false_for_an_episodic_show_with_no_episodes_at_all(conn):
    # A planned show, or a stub not yet fetched — "no data yet" and
    # "genuinely not available" are indistinguishable here by design;
    # confirmed as the accepted behavior, not left untested.
    _add_show(conn, "s-svp007")
    updated = service_presence.refresh_local_presence(conn)
    assert updated == 1
    assert _presence(conn, "s-svp007", "local")["present"] == 0


# --- refresh_catalog_presence (Sonarr/Radarr) ---------------------------------


class _FakeSonarrCatalogClient:
    def __init__(self, series):
        self._series = series

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_series(self):
        return self._series


class _FakeRadarrCatalogClient:
    def __init__(self, movies):
        self._movies = movies

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_movies(self):
        return self._movies


def test_sonarr_presence_true_on_a_confident_title_match(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-svp010", title_romaji="Attack on Titan")
    fake = _FakeSonarrCatalogClient([{"title": "Attack on Titan"}, {"title": "Some Other Show"}])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 1
    assert _presence(conn, "s-svp010", "sonarr")["present"] == 1


def test_sonarr_presence_false_with_no_match_in_the_catalog(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-svp011", title_romaji="Attack on Titan")
    fake = _FakeSonarrCatalogClient([{"title": "Completely Unrelated Show"}])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 1  # a new row, first-seen false
    assert _presence(conn, "s-svp011", "sonarr")["present"] == 0


def test_sonarr_presence_never_checks_a_movie_shaped_show(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-svp012", title_romaji="A Movie", media_shape="movie")
    fake = _FakeSonarrCatalogClient([{"title": "A Movie"}])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 0
    assert _presence(conn, "s-svp012", "sonarr") is None


def test_sonarr_presence_false_with_an_empty_catalog(conn, monkeypatch):
    # An empty catalog (freshly-installed Sonarr, zero series) — best_match
    # itself already returns None for an empty candidate list; confirms
    # that path doesn't error and every tracked show just lands present=False.
    _configure_sonarr()
    _add_show(conn, "s-svp019", title_romaji="A Show")
    monkeypatch.setattr(
        sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrCatalogClient([])
    )

    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 1
    assert _presence(conn, "s-svp019", "sonarr")["present"] == 0


def test_radarr_presence_true_on_a_confident_title_match(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-svp013", title_romaji="Project Hail Mary", media_shape="movie")
    fake = _FakeRadarrCatalogClient([{"title": "Project Hail Mary"}])
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 1
    assert _presence(conn, "s-svp013", "radarr")["present"] == 1


def test_radarr_presence_never_checks_an_episodic_show(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-svp014", title_romaji="A Show")
    fake = _FakeRadarrCatalogClient([{"title": "A Show"}])
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 0
    assert _presence(conn, "s-svp014", "radarr") is None


def test_sonarr_presence_not_configured_is_a_clean_no_op(conn, monkeypatch):
    _add_show(conn, "s-svp015", title_romaji="A Show")
    called = []
    monkeypatch.setattr(
        sonarr_client,
        "SonarrClient",
        lambda *a, **kw: called.append(1) or _FakeSonarrCatalogClient([]),
    )
    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 0
    assert called == []


def test_sonarr_catalog_fetch_failure_is_a_clean_zero_result_and_records_service_health(
    conn, monkeypatch
):
    _configure_sonarr()
    _add_show(conn, "s-svp016", title_romaji="A Show")

    class _BrokenClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def all_series(self):
            raise sonarr_client.SonarrError("boom")

    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _BrokenClient())
    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 0
    health = next(r for r in service_health.get_all(conn) if r["service"] == "sonarr")
    assert health["status"] == "unreachable"
    assert "boom" in health["last_error_message"]


def test_sonarr_and_radarr_presence_both_run_in_one_catalog_sweep(conn, monkeypatch):
    _configure_sonarr()
    _configure_radarr()
    _add_show(conn, "s-svp017", title_romaji="A Show")
    _add_show(conn, "s-svp018", title_romaji="A Movie", media_shape="movie")
    monkeypatch.setattr(
        sonarr_client,
        "SonarrClient",
        lambda *a, **kw: _FakeSonarrCatalogClient([{"title": "A Show"}]),
    )
    monkeypatch.setattr(
        radarr_client,
        "RadarrClient",
        lambda *a, **kw: _FakeRadarrCatalogClient([{"title": "A Movie"}]),
    )
    updated = service_presence.refresh_catalog_presence(conn)
    assert updated == 2
    assert _presence(conn, "s-svp017", "sonarr")["present"] == 1
    assert _presence(conn, "s-svp018", "radarr")["present"] == 1


# --- deep-link backfill (2026-08-18) -------------------------------------------


def test_sonarr_catalog_match_backfills_a_real_deep_link(conn, monkeypatch):
    _configure_sonarr()
    cfg = config.get_current()
    cfg.sonarr_url = "http://sonarr.local"
    _add_show(conn, "s-svp020", title_romaji="Attack on Titan")
    fake = _FakeSonarrCatalogClient(
        [{"title": "Attack on Titan", "titleSlug": "attack-on-titan"}]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    service_presence.refresh_catalog_presence(conn)
    link = _external_id(conn, "s-svp020", "sonarr")
    assert link is not None
    assert link["external_id"] == "attack-on-titan"
    assert link["url"] == "http://sonarr.local/series/attack-on-titan"


def test_radarr_catalog_match_backfills_a_real_deep_link(conn, monkeypatch):
    _configure_radarr()
    cfg = config.get_current()
    cfg.radarr_url = "http://radarr.local"
    _add_show(conn, "s-svp021", title_romaji="Project Hail Mary", media_shape="movie")
    fake = _FakeRadarrCatalogClient(
        [{"title": "Project Hail Mary", "titleSlug": "project-hail-mary"}]
    )
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    service_presence.refresh_catalog_presence(conn)
    link = _external_id(conn, "s-svp021", "radarr")
    assert link is not None
    assert link["external_id"] == "project-hail-mary"
    assert link["url"] == "http://radarr.local/movie/project-hail-mary"


def test_sonarr_catalog_match_with_no_title_slug_writes_no_link(conn, monkeypatch):
    # Older/unusual Sonarr responses missing titleSlug — presence still
    # records correctly, the link is just skipped rather than guessed.
    _configure_sonarr()
    config.get_current().sonarr_url = "http://sonarr.local"
    _add_show(conn, "s-svp022", title_romaji="Attack on Titan")
    fake = _FakeSonarrCatalogClient([{"title": "Attack on Titan"}])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    service_presence.refresh_catalog_presence(conn)
    assert _presence(conn, "s-svp022", "sonarr")["present"] == 1
    assert _external_id(conn, "s-svp022", "sonarr") is None


def test_sonarr_catalog_match_already_linked_by_add_show_with_arr_is_not_duplicated(
    conn, monkeypatch
):
    # A show addShowWithArr already linked — INSERT OR IGNORE means the
    # monthly sweep re-matching it afterward is a silent no-op, not a
    # second row or an overwrite.
    _configure_sonarr()
    config.get_current().sonarr_url = "http://sonarr.local"
    _add_show(conn, "s-svp023", title_romaji="Attack on Titan")
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-svp023', 'sonarr', 'attack-on-titan',"
        " 'http://sonarr.local/series/attack-on-titan', 'x')"
    )
    conn.commit()
    fake = _FakeSonarrCatalogClient(
        [{"title": "Attack on Titan", "titleSlug": "attack-on-titan"}]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    service_presence.refresh_catalog_presence(conn)
    rows = conn.execute(
        "SELECT * FROM show_external_id WHERE show_id = 's-svp023' AND service = 'sonarr'"
    ).fetchall()
    assert len(rows) == 1


def test_sonarr_catalog_no_match_writes_no_link(conn, monkeypatch):
    _configure_sonarr()
    config.get_current().sonarr_url = "http://sonarr.local"
    _add_show(conn, "s-svp024", title_romaji="Attack on Titan")
    fake = _FakeSonarrCatalogClient(
        [{"title": "Completely Unrelated Show", "titleSlug": "unrelated"}]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    service_presence.refresh_catalog_presence(conn)
    assert _external_id(conn, "s-svp024", "sonarr") is None
