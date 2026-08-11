"""Local file audit — SCOPE.md §5.2/§6.10, BUILD_PLAN.md B.3b. Same
real-migrated-SQLite-DB + fake-client approach test_availability.py
(B.3) already established, plus real tmp_path directories for the one
piece that genuinely reads the filesystem (orphan discovery).
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import config, local_audit, radarr_client, service_health, sonarr_client


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "local_audit_test.db"
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


def _add_episode(conn, episode_id, show_id, season=1, episode=1, available="unavailable"):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state,"
        " available_via_sonarr, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', 'unwatched', ?, 'x', 'x')",
        (episode_id, show_id, season, episode, available),
    )
    conn.commit()


def _sonarr_episode(season, episode, has_file, path=None):
    ep = {
        "seasonNumber": season,
        "episodeNumber": episode,
        "hasFile": has_file,
    }
    if has_file:
        ep["episodeFile"] = {"path": path or f"/data/s{season:02d}e{episode:02d}.mkv"}
    return ep


class _FakeSonarrClient:
    def __init__(self, series_list, episodes_by_series_id=None, fail_series_id=None):
        self._series_list = series_list
        self._episodes_by_series_id = episodes_by_series_id or {}
        self._fail_series_id = fail_series_id

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_series(self):
        return self._series_list

    def episodes(self, series_id, include_episode_file=False):
        if series_id == self._fail_series_id:
            raise sonarr_client.SonarrError("boom")
        return self._episodes_by_series_id.get(series_id, [])


class _FakeRadarrClient:
    def __init__(self, movies):
        self._movies = movies

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_movies(self):
        return self._movies


def _radarr_movie(tmdb_id, title, has_file, path=None, movie_file_path=None):
    m = {"tmdbId": tmdb_id, "title": title, "path": path, "hasFile": has_file}
    if has_file:
        m["movieFile"] = {"path": movie_file_path or "/data/movie.mkv"}
    return m


# --- Sonarr reconciliation ---------------------------------------------------


def test_sonarr_corrects_a_missing_available_flag(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-lau001", tvdb_id=457078)
    _add_episode(conn, "e-lau001", "s-lau001", available="unavailable")
    series = [{"id": 1, "tvdbId": 457078, "title": "Test", "path": "/data/show"}]
    episodes = {1: [_sonarr_episode(1, 1, has_file=True, path="/data/show/s01e01.mkv")]}
    fake = _FakeSonarrClient(series, episodes)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert result["episodes_corrected"] == 1
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-lau001'"
    ).fetchone()
    assert row["available_via_sonarr"] == "available"
    assert row["file_path_sonarr"] == "/data/show/s01e01.mkv"


def test_sonarr_corrects_a_stale_available_flag(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-lau002", tvdb_id=457078)
    _add_episode(conn, "e-lau002", "s-lau002", available="available")
    series = [{"id": 1, "tvdbId": 457078, "title": "Test", "path": "/data/show"}]
    episodes = {1: [_sonarr_episode(1, 1, has_file=False)]}
    fake = _FakeSonarrClient(series, episodes)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert result["episodes_corrected"] == 1
    row = conn.execute(
        "SELECT available_via_sonarr, file_path_sonarr FROM episode WHERE id = 'e-lau002'"
    ).fetchone()
    assert row["available_via_sonarr"] == "unavailable"
    assert row["file_path_sonarr"] is None


def test_sonarr_already_correct_state_is_a_no_op(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-lau003", tvdb_id=457078)
    _add_episode(conn, "e-lau003", "s-lau003", available="unavailable")
    series = [{"id": 1, "tvdbId": 457078, "title": "Test", "path": "/data/show"}]
    episodes = {1: [_sonarr_episode(1, 1, has_file=False)]}
    fake = _FakeSonarrClient(series, episodes)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert result["episodes_corrected"] == 0


def test_sonarr_not_configured_is_a_clean_no_op(conn, monkeypatch):
    called = []
    monkeypatch.setattr(
        sonarr_client, "SonarrClient", lambda *a, **kw: called.append(1) or _FakeSonarrClient([])
    )
    result = local_audit._audit_sonarr(conn)
    assert result == {"episodes_corrected": 0, "orphan_files": [], "untracked_shows": []}
    assert called == []


def test_sonarr_untracked_show_is_listed_without_walking_its_folder(conn, monkeypatch):
    _configure_sonarr()
    # No show in LCARS at all for this tvdb id.
    series = [
        {
            "id": 1,
            "tvdbId": 999999,
            "title": "Unknown Show",
            "path": "/data/unknown",
            "seriesType": "standard",
        }
    ]
    fake = _FakeSonarrClient(series, episodes_by_series_id={})
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert result["untracked_shows"] == [
        {
            "service": "sonarr",
            "title": "Unknown Show",
            "external_id": 999999,
            "path": "/data/unknown",
            # B.11d — no "seasons" array on this fake series at all, so
            # the [1] fallback applies (all_sonarr_series_with_seasons's
            # own docstring).
            "season_numbers": [1],
            # B.11d — passed through raw for show_backfill.py's own
            # trackingSpace classification, not exposed on the GraphQL
            # UntrackedShow type.
            "series_type": "standard",
        }
    ]
    # episodes() was never called for it — series 1 has no entry in episodes_by_series_id,
    # and if it had been called it would have raised KeyError-free empty list anyway, so
    # the real assertion is on the untracked_shows list above being the only finding.
    assert result["orphan_files"] == []


def test_sonarr_skips_an_unfetched_episode(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-lau004", tvdb_id=457078)
    # Show tracked, but this episode was never fetched into LCARS at all.
    series = [{"id": 1, "tvdbId": 457078, "title": "Test", "path": "/data/show"}]
    episodes = {1: [_sonarr_episode(5, 99, has_file=True)]}
    fake = _FakeSonarrClient(series, episodes)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert result["episodes_corrected"] == 0


def test_sonarr_client_error_mid_walk_keeps_earlier_partial_results(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-lau005", tvdb_id=1)
    _add_episode(conn, "e-lau005", "s-lau005", available="unavailable")
    # Both series must be *tracked* (known_tvdb_ids) — an untracked series
    # hits the loop's own "continue" before ever calling client.episodes(),
    # so fail_series_id=2 would silently never fire otherwise (a real gap
    # caught while adding B.6's own health-recording assertion below).
    _add_show(conn, "s-lau099", tvdb_id=2)
    series = [
        {"id": 1, "tvdbId": 1, "title": "First", "path": "/data/first"},
        {"id": 2, "tvdbId": 2, "title": "Second", "path": "/data/second"},
    ]
    episodes = {1: [_sonarr_episode(1, 1, has_file=True, path="/data/first/e1.mkv")]}
    fake = _FakeSonarrClient(series, episodes, fail_series_id=2)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    # series 1 was processed and committed before series 2's failure was hit.
    assert result["episodes_corrected"] == 1
    row = conn.execute("SELECT available_via_sonarr FROM episode WHERE id = 'e-lau005'").fetchone()
    assert row["available_via_sonarr"] == "available"
    # §6.7, B.6 — the failure surfaced somewhere in the walk still records
    # the whole pass as unreachable, even though partial results were kept.
    health = next(r for r in service_health.get_all(conn) if r["service"] == "sonarr")
    assert health["status"] == "unreachable"


# --- Sonarr orphan discovery (real filesystem, via tmp_path) ----------------


def test_sonarr_orphan_file_is_found_and_season_episode_parsed(conn, monkeypatch, tmp_path):
    _configure_sonarr()
    _add_show(conn, "s-lau006", tvdb_id=457078)
    _add_episode(conn, "e-lau006", "s-lau006", available="available")
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    known_file = show_dir / "Test Show - S01E01 - Known [1080p].mkv"
    known_file.write_bytes(b"x")
    orphan_file = show_dir / "Test Show - S01E02 - Orphan [1080p].mkv"
    orphan_file.write_bytes(b"x")
    (show_dir / "Test Show - S01E01 - Known [1080p].srt").write_bytes(b"subs")  # not a video ext

    series = [{"id": 1, "tvdbId": 457078, "title": "Test Show", "path": str(show_dir)}]
    episodes = {1: [_sonarr_episode(1, 1, has_file=True, path=str(known_file))]}
    fake = _FakeSonarrClient(series, episodes)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert len(result["orphan_files"]) == 1
    orphan = result["orphan_files"][0]
    assert orphan["show_id"] == "s-lau006"
    assert orphan["path"] == str(orphan_file)
    assert orphan["parsed_season"] == 1
    assert orphan["parsed_episode"] == 2


def test_sonarr_orphan_with_unparseable_filename_is_still_reported(conn, monkeypatch, tmp_path):
    _configure_sonarr()
    _add_show(conn, "s-lau007", tvdb_id=457078)
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    weird_file = show_dir / "some_random_file.mkv"
    weird_file.write_bytes(b"x")

    series = [{"id": 1, "tvdbId": 457078, "title": "Test Show", "path": str(show_dir)}]
    fake = _FakeSonarrClient(series, {1: []})
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert len(result["orphan_files"]) == 1
    assert result["orphan_files"][0]["parsed_season"] is None
    assert result["orphan_files"][0]["parsed_episode"] is None


def test_sonarr_inaccessible_path_is_gracefully_skipped_not_an_error(conn, monkeypatch):
    _configure_sonarr()
    _add_show(conn, "s-lau008", tvdb_id=457078)
    series = [{"id": 1, "tvdbId": 457078, "title": "Test Show", "path": "/does/not/exist/anywhere"}]
    fake = _FakeSonarrClient(series, {1: []})
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_sonarr(conn)
    assert result["orphan_files"] == []


# --- Radarr reconciliation ---------------------------------------------------


def test_radarr_corrects_a_missing_available_flag(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-lau101", tmdb_id=687163, media_shape="movie")
    movies = [_radarr_movie(687163, "Project Hail Mary", has_file=True, movie_file_path="/m.mkv")]
    fake = _FakeRadarrClient(movies)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_radarr(conn)
    assert result["shows_corrected"] == 1
    row = conn.execute(
        "SELECT available_via_radarr, file_path_radarr FROM show WHERE id = 's-lau101'"
    ).fetchone()
    assert row["available_via_radarr"] == "available"
    assert row["file_path_radarr"] == "/m.mkv"
    health = next(r for r in service_health.get_all(conn) if r["service"] == "radarr")
    assert health["status"] == "ok"


def test_radarr_client_error_records_unreachable_service_health(conn, monkeypatch):
    _configure_radarr()

    class _BrokenRadarrClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def all_movies(self):
            raise radarr_client.RadarrError("boom")

    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: _BrokenRadarrClient())
    result = local_audit._audit_radarr(conn)
    assert result["shows_corrected"] == 0
    health = next(r for r in service_health.get_all(conn) if r["service"] == "radarr")
    assert health["status"] == "unreachable"
    assert "boom" in health["last_error_message"]


def test_radarr_corrects_a_stale_available_flag(conn, monkeypatch):
    _configure_radarr()
    _add_show(conn, "s-lau102", tmdb_id=687163, media_shape="movie")
    conn.execute(
        "UPDATE show SET available_via_radarr = 'available', file_path_radarr = '/old.mkv'"
        " WHERE id = 's-lau102'"
    )
    conn.commit()
    movies = [_radarr_movie(687163, "Project Hail Mary", has_file=False)]
    fake = _FakeRadarrClient(movies)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_radarr(conn)
    assert result["shows_corrected"] == 1
    row = conn.execute(
        "SELECT available_via_radarr, file_path_radarr FROM show WHERE id = 's-lau102'"
    ).fetchone()
    assert row["available_via_radarr"] == "unavailable"
    assert row["file_path_radarr"] is None


def test_radarr_not_configured_is_a_clean_no_op(conn, monkeypatch):
    called = []
    monkeypatch.setattr(
        radarr_client, "RadarrClient", lambda *a, **kw: called.append(1) or _FakeRadarrClient([])
    )
    result = local_audit._audit_radarr(conn)
    assert result == {"shows_corrected": 0, "orphan_files": [], "untracked_shows": []}
    assert called == []


def test_radarr_untracked_movie_is_listed(conn, monkeypatch):
    _configure_radarr()
    movies = [_radarr_movie(999999, "Unknown Movie", has_file=True, path="/data/unknown")]
    fake = _FakeRadarrClient(movies)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_radarr(conn)
    assert result["untracked_shows"] == [
        {
            "service": "radarr",
            "title": "Unknown Movie",
            "external_id": 999999,
            "path": "/data/unknown",
        }
    ]


def test_radarr_orphan_file_is_found(conn, monkeypatch, tmp_path):
    _configure_radarr()
    _add_show(conn, "s-lau103", tmdb_id=687163, media_shape="movie")
    movie_dir = tmp_path / "movie"
    movie_dir.mkdir()
    known_file = movie_dir / "known.mkv"
    known_file.write_bytes(b"x")
    orphan_file = movie_dir / "orphan (Director's Cut).mkv"
    orphan_file.write_bytes(b"x")

    movies = [
        _radarr_movie(
            687163,
            "Project Hail Mary",
            has_file=True,
            path=str(movie_dir),
            movie_file_path=str(known_file),
        )
    ]
    fake = _FakeRadarrClient(movies)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)

    result = local_audit._audit_radarr(conn)
    assert len(result["orphan_files"]) == 1
    assert result["orphan_files"][0]["path"] == str(orphan_file)
    assert result["orphan_files"][0]["show_id"] == "s-lau103"


# --- combined public entry point --------------------------------------------


def test_audit_local_files_combines_both_services(conn, monkeypatch):
    _configure_sonarr()
    _configure_radarr()
    _add_show(conn, "s-lau201", tvdb_id=1)
    _add_episode(conn, "e-lau201", "s-lau201", available="unavailable")
    _add_show(conn, "s-lau202", tmdb_id=2, media_shape="movie")

    sonarr_series = [{"id": 1, "tvdbId": 1, "title": "A", "path": "/data/a"}]
    sonarr_episodes = {1: [_sonarr_episode(1, 1, has_file=True, path="/data/a/e1.mkv")]}
    sonarr_fake = _FakeSonarrClient(sonarr_series, sonarr_episodes)
    radarr_fake = _FakeRadarrClient(
        [_radarr_movie(2, "B", has_file=True, movie_file_path="/data/b/m.mkv")]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: sonarr_fake)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: radarr_fake)

    result = local_audit.audit_local_files(conn)
    assert result["episodes_corrected"] == 1
    assert result["shows_corrected"] == 1
    assert result["orphan_files"] == []
    assert result["untracked_shows"] == []


# --- known_anilist_ids / all_sonarr_series_with_seasons (B.11d) --------------


def test_known_anilist_ids_includes_show_level_links(conn):
    _add_show(conn, "s-lau301")
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-lau301', 'anilist', '111', 'https://x', 'x')"
    )
    conn.commit()
    assert local_audit.known_anilist_ids(conn) == {"111"}


def test_known_anilist_ids_includes_season_level_links(conn):
    _add_show(conn, "s-lau302")
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id, source,"
        " created_at, updated_at)"
        " VALUES ('z-lau3s2', 's-lau302', 2, 222, 'fribb', 'x', 'x')"
    )
    conn.commit()
    assert local_audit.known_anilist_ids(conn) == {"222"}


def test_known_anilist_ids_is_empty_with_nothing_linked(conn):
    assert local_audit.known_anilist_ids(conn) == set()


def test_all_sonarr_series_with_seasons_includes_season_zero(conn, monkeypatch):
    # Season 0 (Sonarr's own "specials" bucket) is NOT excluded — a
    # live check (Chainsaw Man: Reze-hen) showed Fribb tags a real
    # movie/OVA entry as season 0 as often as a numbered season.
    _configure_sonarr()
    series = [
        {
            "id": 1,
            "tvdbId": 424536,
            "title": "Frieren",
            "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2}],
        }
    ]
    fake = _FakeSonarrClient(series)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    result = local_audit.all_sonarr_series_with_seasons(conn)
    assert result == [{"tvdb_id": 424536, "season_numbers": [0, 1, 2]}]


def test_all_sonarr_series_with_seasons_falls_back_to_season_one(conn, monkeypatch):
    _configure_sonarr()
    series = [{"id": 1, "tvdbId": 111, "title": "No Seasons Array"}]
    fake = _FakeSonarrClient(series)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    result = local_audit.all_sonarr_series_with_seasons(conn)
    assert result == [{"tvdb_id": 111, "season_numbers": [1]}]


def test_all_sonarr_series_with_seasons_not_configured_is_a_clean_no_op(conn):
    assert local_audit.all_sonarr_series_with_seasons(conn) == []


# --- find_untracked_shows_readonly_by_source (B.11e follow-up) ---------------
#
# The `reported` set untracked_sweep.py's own pruning gate depends on —
# a real bug found in review: the original sweep couldn't tell "genuinely
# nothing untracked" apart from "couldn't reach this source this time,"
# since both surfaced as the same empty contribution to the combined
# list.


class _FailingSonarrClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_series(self):
        raise sonarr_client.SonarrError("boom")


class _FailingRadarrClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_movies(self):
        raise radarr_client.RadarrError("boom")


def test_by_source_reports_sonarr_success(conn, monkeypatch):
    _configure_sonarr()
    fake = _FakeSonarrClient(
        [{"id": 1, "tvdbId": 111, "title": "Untracked", "path": "/data/untracked"}]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    result = local_audit.find_untracked_shows_readonly_by_source(conn)
    assert "sonarr" in result["reported"]
    assert len(result["entries"]) == 1


def test_by_source_reports_sonarr_not_configured_as_reported(conn):
    # Deliberate, stable "nothing to report from here" — safe to prune
    # findings against, not the same as a real failure.
    result = local_audit.find_untracked_shows_readonly_by_source(conn)
    assert "sonarr" in result["reported"]
    assert result["entries"] == []


def test_by_source_does_not_report_sonarr_on_a_real_failure(conn, monkeypatch):
    _configure_sonarr()
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FailingSonarrClient())
    result = local_audit.find_untracked_shows_readonly_by_source(conn)
    assert "sonarr" not in result["reported"]
    assert result["entries"] == []  # still swallowed into an empty contribution


def test_by_source_reports_radarr_not_configured_as_reported(conn):
    result = local_audit.find_untracked_shows_readonly_by_source(conn)
    assert "radarr" in result["reported"]


def test_by_source_does_not_report_radarr_on_a_real_failure(conn, monkeypatch):
    _configure_radarr()
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: _FailingRadarrClient())
    result = local_audit.find_untracked_shows_readonly_by_source(conn)
    assert "radarr" not in result["reported"]
    assert result["entries"] == []


def test_find_untracked_shows_readonly_is_still_just_the_flat_list(conn, monkeypatch):
    # The pre-existing, unchanged contract every other caller relies on.
    _configure_sonarr()
    fake = _FakeSonarrClient(
        [{"id": 1, "tvdbId": 111, "title": "Untracked", "path": "/data/untracked"}]
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    assert local_audit.find_untracked_shows_readonly(conn) == (
        local_audit.find_untracked_shows_readonly_by_source(conn)["entries"]
    )
