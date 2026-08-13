"""One-time/repeatable show backfill — SCOPE.md §5.1/§5.2, BUILD_PLAN.md
B.11d. Same real-migrated-SQLite-DB + fake-client approach
test_local_audit.py already established. preview_backfill() reuses
local_audit.find_untracked_shows_readonly() (genuinely read-only) plus
this module's own _find_untracked_anilist_entries();
backfill_untracked_shows() reuses the fuller audit_local_files().
fribb.load_dataset is monkeypatched throughout, same pattern
test_server.py's own reconcileSeasonMapping tests already established
(§5.5's own automatic id-mapper reconciliation section) — no real
network call.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, fribb, show_backfill, sonarr_client


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


class _FailingFakeSonarrClient:
    """B.11e follow-up — a real, configured Sonarr connection that
    fails outright, for preview_backfill_with_status()'s own
    reported_services test. Distinct from "not configured" (config.py
    has no sonarr_url/sonarr_api_key at all), which is a deliberate,
    stable zero, not a failure."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def all_series(self):
        raise sonarr_client.SonarrError("boom")


def _sonarr_series(series_id, tvdb_id, title, series_type=None, path=None):
    return {
        "id": series_id,
        "tvdbId": tvdb_id,
        "title": title,
        "seriesType": series_type,
        "path": path,
    }


def _patch_fribb(monkeypatch, dataset):
    monkeypatch.setattr(fribb, "load_dataset", lambda: dataset)


TVDB_INDEX_EMPTY = fribb.build_tvdb_index([])


def _sweep(conn):
    return show_backfill._find_untracked_anilist_entries(conn, show_backfill._fribb_tvdb_index())


# --- classification (pure-ish — Fribb dataset is the only input) -------------


def test_classify_sonarr_uses_fribb_resolution_not_series_type(monkeypatch):
    # Live-verified finding, 2026-08-10: Sonarr's own seriesType is NOT
    # a reliable anime signal for this user's real library (Frieren,
    # DAN DA DAN, etc. all report "standard"). A Fribb match is the
    # real signal — series_type is irrelevant to the outcome now.
    dataset = [{"tvdb_id": 111, "anilist_id": 999, "mal_id": None, "season": {"tvdb": 1}}]
    _patch_fribb(monkeypatch, dataset)
    index = fribb.build_tvdb_index(dataset)
    entry = {"service": "sonarr", "title": "Frieren", "external_id": 111, "series_type": "standard"}
    result = show_backfill._classify(entry, index)
    assert result["tracking_space"] == "anime"
    assert result["media_shape"] == "episodic"
    assert result["anilist_id"] == 999
    assert result["tvdb_id"] == 111


def test_classify_sonarr_with_no_fribb_match_is_tv(monkeypatch):
    entry = {"service": "sonarr", "title": "NCIS", "external_id": 222, "series_type": "standard"}
    result = show_backfill._classify(entry, TVDB_INDEX_EMPTY)
    assert result["tracking_space"] == "tv"
    assert result["anilist_id"] is None


def test_classify_sonarr_checks_every_season_not_just_season_one(monkeypatch):
    # Live-verified finding, 2026-08-10 (real Frieren data: Fribb
    # resolves tvdb 424536's season 1 and season 2 to two genuinely
    # different AniList ids). The show-level anilistId still comes
    # from season 1 only (matching _ensure_anilist_link's own
    # established convention) — this test's real point is that
    # trackingSpace still correctly lands on 'anime' by checking every
    # season, not just the one used for the show-level link.
    dataset = [
        {"tvdb_id": 424536, "anilist_id": 154587, "mal_id": None, "season": {"tvdb": 1}},
        {"tvdb_id": 424536, "anilist_id": 182255, "mal_id": None, "season": {"tvdb": 2}},
    ]
    _patch_fribb(monkeypatch, dataset)
    index = fribb.build_tvdb_index(dataset)
    entry = {
        "service": "sonarr",
        "title": "Frieren",
        "external_id": 424536,
        "season_numbers": [1, 2],
    }
    result = show_backfill._classify(entry, index)
    assert result["tracking_space"] == "anime"
    assert result["anilist_id"] == 154587  # season 1's own resolution, not season 2's


def test_classify_radarr_always_defaults_to_tv():
    # No anime signal exists from Radarr at all — documented cut, B.11d.
    entry = {"service": "radarr", "title": "Movie A", "external_id": 10}
    result = show_backfill._classify(entry, TVDB_INDEX_EMPTY)
    assert result["tracking_space"] == "tv"
    assert result["media_shape"] == "movie"
    assert result["tmdb_id"] == 10


def test_classify_anilist_maps_format_to_media_shape():
    entry = {
        "service": "anilist",
        "title": "A Movie",
        "external_id": 555,
        "format": "MOVIE",
        "status": "COMPLETED",
    }
    result = show_backfill._classify(entry, TVDB_INDEX_EMPTY)
    assert result["tracking_space"] == "anime"
    assert result["media_shape"] == "movie"
    assert result["anilist_id"] == 555


def test_classify_anilist_null_format_defaults_to_episodic():
    entry = {
        "service": "anilist",
        "title": "Unknown Format",
        "external_id": 556,
        "format": None,
        "status": "PLANNING",
    }
    assert show_backfill._classify(entry, TVDB_INDEX_EMPTY)["media_shape"] == "episodic"


# --- _find_untracked_anilist_entries ------------------------------------------


def test_anilist_sweep_excludes_already_tracked_show_level(conn, monkeypatch):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-trck01', 'episodic', 'anime', 'Tracked', 'romaji', 'watching', 1, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-trck01', 'anilist', '101', 'https://x', 'x')"
    )
    conn.commit()
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 101, "format": "TV", "status": "CURRENT", "title": "Tracked"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == []


def test_anilist_sweep_excludes_a_season_level_link(conn, monkeypatch):
    # §5.5 — a split-cour sequel's own AniList link can live on the
    # season row instead of show_external_id.
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-trck02', 'episodic', 'anime', 'Tracked S2', 'romaji', 'watching', 1, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, source, created_at, updated_at)"
        " VALUES ('z-seas01', 's-trck02', 2, 202, 'fribb', 'x', 'x')"
    )
    conn.commit()
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 202, "format": "TV", "status": "CURRENT", "title": "Tracked S2"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == []


def test_anilist_sweep_excludes_an_entry_whose_tvdb_id_is_in_sonarrs_catalog(conn, monkeypatch):
    # Fribb resolves this AniList entry back to a tvdb id Sonarr's own
    # catalog already has (tracked or not) — Sonarr's own sweep's job,
    # not this one's, to avoid a duplicate show for a split-cour sequel.
    _configure_sonarr()
    dataset = [{"tvdb_id": 333, "anilist_id": 303, "mal_id": None, "season": {"tvdb": 1}}]
    _patch_fribb(monkeypatch, dataset)
    monkeypatch.setattr(
        sonarr_client,
        "SonarrClient",
        lambda *a, **kw: _FakeSonarrClient([_sonarr_series(1, 333, "Some Show")]),
    )
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 303, "format": "TV", "status": "CURRENT", "title": "Some Show"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == []


def test_anilist_sweep_excludes_a_second_season_fribb_only_resolves_via_season_number(
    conn, monkeypatch
):
    # The real Frieren-shaped case, live-verified 2026-08-10: season 1
    # and season 2 resolve to two *different* AniList ids. A season-1-
    # only dedup check would have missed season 2 entirely and let it
    # through as a false "untracked" duplicate.
    _configure_sonarr()
    dataset = [
        {"tvdb_id": 424536, "anilist_id": 154587, "mal_id": None, "season": {"tvdb": 1}},
        {"tvdb_id": 424536, "anilist_id": 182255, "mal_id": None, "season": {"tvdb": 2}},
    ]
    _patch_fribb(monkeypatch, dataset)
    series = [
        {
            "id": 1,
            "tvdbId": 424536,
            "title": "Frieren",
            "seriesType": "standard",
            "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2}],
        }
    ]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 182255, "format": "TV", "status": "CURRENT", "title": "Frieren S2"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == []


def test_anilist_sweep_excludes_a_season_zero_tagged_movie(conn, monkeypatch):
    # The real Chainsaw Man-shaped case, live-verified 2026-08-10:
    # Fribb tags "Chainsaw Man: Reze-hen" (a real movie on the user's
    # own AniList list) as season 0 under Chainsaw Man's own tvdb id —
    # excluding season 0 from the per-season scan (this module's own
    # earlier version) left it undetected as "already accounted for."
    _configure_sonarr()
    dataset = [
        {"tvdb_id": 397934, "anilist_id": 127230, "mal_id": None, "season": {"tvdb": 1}},
        {"tvdb_id": 397934, "anilist_id": 171627, "mal_id": None, "season": {"tvdb": 0}},
    ]
    _patch_fribb(monkeypatch, dataset)
    series = [
        {
            "id": 1,
            "tvdbId": 397934,
            "title": "Chainsaw Man",
            "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
        }
    ]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))
    config.get_current().anilist_access_token = "tok"
    my_list = [
        {"anilist_id": 171627, "format": "MOVIE", "status": "COMPLETED", "title": "Reze-hen"}
    ]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == []


def test_anilist_sweep_excludes_music_format(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 404, "format": "MUSIC", "status": "COMPLETED", "title": "A Song"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == []


def test_anilist_sweep_includes_a_genuinely_untracked_entry(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 505, "format": "TV", "status": "PLANNING", "title": "New Show"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    result = _sweep(conn)
    assert result == [
        {
            "service": "anilist",
            "title": "New Show",
            "external_id": 505,
            "format": "TV",
            "status": "PLANNING",
        }
    ]


def test_anilist_sweep_is_a_clean_no_op_without_a_token(conn):
    assert _sweep(conn) == []


def test_anilist_sweep_swallows_an_anilist_error(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"

    def _raise(token, **kw):
        raise anilist_client.AniListError("boom")

    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", _raise)
    assert _sweep(conn) == []


# --- _find_untracked_anilist_entries_by_source (B.11e follow-up) -------------
#
# Same "reported" signal local_audit.find_untracked_shows_readonly_by_source
# gained, for the AniList sweep's own real bug found in review: a bare
# `except AniListError: return []` was indistinguishable from "genuinely
# nothing untracked" — untracked_sweep.py's own pruning needs the two
# told apart.


def _sweep_by_source(conn):
    return show_backfill._find_untracked_anilist_entries_by_source(
        conn, show_backfill._fribb_tvdb_index()
    )


def test_anilist_sweep_by_source_reports_success(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 505, "format": "TV", "status": "PLANNING", "title": "New Show"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    result = _sweep_by_source(conn)
    assert result["reported"] is True
    assert len(result["entries"]) == 1


def test_anilist_sweep_by_source_reports_no_token_as_reported(conn):
    # Deliberate, stable "nothing to report from here" — safe to prune
    # findings against, not the same as a real failure.
    result = _sweep_by_source(conn)
    assert result["reported"] is True
    assert result["entries"] == []


def test_anilist_sweep_by_source_does_not_report_on_a_real_failure(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"

    def _raise(token, **kw):
        raise anilist_client.AniListError("boom")

    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", _raise)
    result = _sweep_by_source(conn)
    assert result["reported"] is False
    assert result["entries"] == []  # still swallowed into an empty contribution


def test_find_untracked_anilist_entries_is_still_just_the_flat_list(conn, monkeypatch):
    # The pre-existing, unchanged contract every other caller relies on.
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 505, "format": "TV", "status": "PLANNING", "title": "New Show"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    assert _sweep(conn) == _sweep_by_source(conn)["entries"]


# --- preview_backfill (dry-run) -----------------------------------------------


def test_preview_backfill_lists_untracked_items_without_writing(conn, monkeypatch):
    _configure_sonarr()
    dataset = [{"tvdb_id": 111, "anilist_id": 999, "mal_id": None, "season": {"tvdb": 1}}]
    _patch_fribb(monkeypatch, dataset)
    series = [_sonarr_series(1, 111, "Untracked Show")]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))

    preview = show_backfill.preview_backfill(conn)
    assert preview == [
        {
            "service": "sonarr",
            "title": "Untracked Show",
            "external_id": 111,
            "path": None,
            "tracking_space": "anime",
            "media_shape": "episodic",
        }
    ]
    # Genuinely read-only: no show created, and no service_health record
    # either (unlike audit_local_files()'s own equivalent pass) — a Query
    # must stay side-effect free.
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM service_health").fetchone()[0] == 0


def test_preview_backfill_carries_the_sonarr_path_through(conn, monkeypatch):
    """B.11e's own sweep needs `path` on every preview item to populate
    `untracked_show_finding.path` — not exposed on `BackfillPreviewItem`
    itself, but must be present in the underlying dict either way."""
    _configure_sonarr()
    dataset = [{"tvdb_id": 111, "anilist_id": 999, "mal_id": None, "season": {"tvdb": 1}}]
    _patch_fribb(monkeypatch, dataset)
    series = [_sonarr_series(1, 111, "Untracked Show", path="/data/anime/Untracked Show")]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))

    preview = show_backfill.preview_backfill(conn)
    assert preview[0]["path"] == "/data/anime/Untracked Show"


def test_preview_backfill_is_empty_with_nothing_untracked(conn, monkeypatch):
    _configure_sonarr()
    _patch_fribb(monkeypatch, [])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient([]))
    assert show_backfill.preview_backfill(conn) == []


def test_preview_backfill_with_status_reports_every_source_by_default(conn, monkeypatch):
    # Nothing configured at all — still "reported" for all three
    # (deliberate, stable zero from each source, not a failure), the
    # shape untracked_sweep.py's own pruning gate depends on.
    _patch_fribb(monkeypatch, [])
    result = show_backfill.preview_backfill_with_status(conn)
    assert result["items"] == []
    assert result["reported_services"] == {"sonarr", "radarr", "anilist"}


def test_preview_backfill_with_status_excludes_a_source_that_failed(conn, monkeypatch):
    _configure_sonarr()
    _patch_fribb(monkeypatch, [])
    monkeypatch.setattr(
        sonarr_client,
        "SonarrClient",
        lambda *a, **kw: _FailingFakeSonarrClient(),
    )
    result = show_backfill.preview_backfill_with_status(conn)
    assert "sonarr" not in result["reported_services"]
    assert "radarr" in result["reported_services"]


def test_preview_backfill_includes_anilist_sweep_entries(conn, monkeypatch):
    _patch_fribb(monkeypatch, [])
    config.get_current().anilist_access_token = "tok"
    my_list = [
        {"anilist_id": 606, "format": "MOVIE", "status": "COMPLETED", "title": "Streamed Movie"}
    ]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    preview = show_backfill.preview_backfill(conn)
    assert preview == [
        {
            "service": "anilist",
            "title": "Streamed Movie",
            "external_id": 606,
            "path": None,
            "tracking_space": "anime",
            "media_shape": "movie",
        }
    ]


# --- backfill_untracked_shows (the real run) ----------------------------------


def test_backfill_creates_a_show_per_untracked_item(conn, monkeypatch):
    _configure_sonarr()
    dataset = [{"tvdb_id": 222, "anilist_id": 888, "mal_id": None, "season": {"tvdb": 1}}]
    _patch_fribb(monkeypatch, dataset)
    series = [
        _sonarr_series(1, 111, "Show A"),  # no Fribb match — tv
        _sonarr_series(2, 222, "Show B"),  # Fribb match — anime
    ]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))

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


def test_backfill_creates_an_anilist_sweep_show_with_known_status(conn, monkeypatch):
    _patch_fribb(monkeypatch, [])
    config.get_current().anilist_access_token = "tok"
    my_list = [{"anilist_id": 707, "format": "TV", "status": "COMPLETED", "title": "AniList Only"}]
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token, **kw: my_list)
    fetch_status_calls = []
    monkeypatch.setattr(
        anilist_client,
        "fetch_my_list_status",
        lambda *a, **kw: fetch_status_calls.append(1) or "SHOULD_NOT_BE_CALLED",
    )

    result = show_backfill.backfill_untracked_shows(conn)
    assert len(result["created"]) == 1
    row = conn.execute("SELECT status FROM show WHERE title_romaji = 'AniList Only'").fetchone()
    assert row["status"] == "completed"
    # The sweep already knew the status from MediaListCollection — no
    # extra live fetch_my_list_status() read needed.
    assert fetch_status_calls == []


def test_backfill_is_idempotent_on_rerun(conn, monkeypatch):
    _configure_sonarr()
    _patch_fribb(monkeypatch, [])
    series = [_sonarr_series(1, 111, "Show A")]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))

    first = show_backfill.backfill_untracked_shows(conn)
    assert len(first["created"]) == 1

    second = show_backfill.backfill_untracked_shows(conn)
    # already tracked now — local_audit's own known-id lookup excludes it
    assert second["created"] == []
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 1


def test_backfill_promotes_a_stub_a_relation_walk_created_mid_run_instead_of_duplicating(
    conn, monkeypatch
):
    """B.11d follow-up, real bug found in the live backfill run: 86
    anilist_id collision pairs, "Mebius Dust" the traced-down example
    (s-7zvsg2/s-e1a4yk, same anilist_id, one row each). Reproduces the
    exact shape: `candidates` is one fixed list computed before this
    loop starts; Show A's own inline metadata fetch (create_show's
    fetch_and_populate) walks AniList relations and auto-creates a
    tracked=false stub for Show B *while the loop is still running* —
    dedup computed once up front can't see that write. Show B's own
    turn, later in the same fixed list, must find and promote that
    stub instead of inserting a second `show` row for the same
    anilist_id."""
    _configure_sonarr()
    dataset = [
        {"tvdb_id": 111, "anilist_id": 900, "mal_id": None, "season": {"tvdb": 1}},
        {"tvdb_id": 222, "anilist_id": 950, "mal_id": None, "season": {"tvdb": 1}},
    ]
    _patch_fribb(monkeypatch, dataset)
    series = [
        _sonarr_series(1, 111, "Show A"),
        _sonarr_series(2, 222, "Show B"),
    ]
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: _FakeSonarrClient(series))

    fake_media_by_id = {
        900: {
            "title": {"romaji": "Show A"},
            "idMal": None,
            "relations": {
                "edges": [
                    {
                        "node": {
                            "id": 950,
                            "idMal": None,
                            "format": "TV",
                            "title": {"romaji": "Show B", "english": None, "native": None},
                        }
                    }
                ]
            },
        },
        950: {"title": {"romaji": "Show B"}, "idMal": None},
    }
    monkeypatch.setattr(
        anilist_client, "fetch_media", lambda anilist_id, **kw: fake_media_by_id.get(anilist_id)
    )
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)

    result = show_backfill.backfill_untracked_shows(conn)
    assert result["failed"] == []
    assert [c["service"] for c in result["created"]] == ["sonarr"]  # Show A only, a real new row
    assert [p["service"] for p in result["promoted"]] == ["sonarr"]  # Show B — the stub, promoted

    rows = conn.execute(
        "SELECT id, title_romaji, tracked FROM show ORDER BY title_romaji"
    ).fetchall()
    assert [r["title_romaji"] for r in rows] == ["Show A", "Show B"]  # not three rows
    assert rows[1]["tracked"] == 1  # promoted, not left as a bare stub

    anilist_links = conn.execute(
        "SELECT show_id, external_id FROM show_external_id WHERE service = 'anilist'"
    ).fetchall()
    assert {r["external_id"] for r in anilist_links} == {"900", "950"}  # no id shared by two rows


def test_backfill_no_longer_has_its_own_throttle_mechanism():
    """2026-08-13 — this module's own estimate-based, per-show throttle
    was removed once `anilist_client.py` grew a real one at its actual
    `_graphql_request` choke point (that module's own comment has the
    full story; see also the now-superseded paragraph this module's
    own docstring used to carry). A pin, not a behavior test: the real
    throttling behavior itself is covered directly in
    test_anilist_client.py, against the module that now actually owns
    it."""
    assert not hasattr(show_backfill, "ANILIST_SECONDS_PER_CALL")
    assert not hasattr(show_backfill, "_anilist_call_estimate")


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


def test_seed_status_known_status_skips_the_live_read(conn, monkeypatch):
    config.get_current().anilist_access_token = "tok"
    _bare_anime_show(conn, "s-seed07", anilist_id=999)
    calls = []
    monkeypatch.setattr(
        anilist_client, "fetch_my_list_status", lambda *a, **kw: calls.append(1) or "COMPLETED"
    )
    show_backfill._seed_status_from_anilist(conn, "s-seed07", known_status="DROPPED")
    row = conn.execute("SELECT status FROM show WHERE id = ?", ("s-seed07",)).fetchone()
    assert row["status"] == "dropped"  # known_status honored, not the live-fetch stub's value
    assert calls == []  # no live read made at all


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
