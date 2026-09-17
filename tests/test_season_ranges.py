"""S2 — season absolute-episode ranges + season_external_id dual-write.

Same real-migrated-SQLite-DB approach as test_watch_reconcile.py / test_show_merge.py.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from lcars import config, season_ranges

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_season_ranges.py"
_spec = importlib.util.spec_from_file_location("backfill_season_ranges", _SCRIPT)
_backfill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backfill_mod)
compute_season_ranges = _backfill_mod.compute_season_ranges
check_anilist_width = _backfill_mod.check_anilist_width


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "season_ranges_test.db"
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHOW_COUNTER = 0


def _show(conn, show_id="s-aaaaaa", title="Test Show", tracking_space="anime"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji,"
        " title_english, primary_title, status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, ?, ?, 'romaji', 'watching', 1, 'x', 'x')",
        (show_id, tracking_space, title, title),
    )
    return show_id


def _season(conn, season_id, show_id, season_number, anilist_id=None, mal_id=None):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id, mal_id,"
        " source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number, anilist_id, mal_id),
    )
    return season_id


def _episode(conn, episode_id, show_id, season, episode, absolute_number=None, kind="regular"):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind,"
        " absolute_number, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'unwatched', 'x', 'x')",
        (episode_id, show_id, season, episode, kind, absolute_number),
    )


# ---------------------------------------------------------------------------
# upsert_season_external_id
# ---------------------------------------------------------------------------


class TestUpsertSeasonExternalId:
    def test_inserts_anilist_and_mal(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100, mal_id=200)
        conn.commit()

        season_ranges.upsert_season_external_id(conn, "z-aaaaaa", 100, 200, "now")
        conn.commit()

        rows = conn.execute(
            "SELECT service, external_id FROM season_external_id"
            " WHERE season_id = 'z-aaaaaa' ORDER BY service"
        ).fetchall()
        assert len(rows) == 2
        assert (rows[0]["service"], rows[0]["external_id"]) == ("anilist", "100")
        assert (rows[1]["service"], rows[1]["external_id"]) == ("mal", "200")

    def test_upsert_overwrites_existing(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        conn.commit()

        season_ranges.upsert_season_external_id(conn, "z-aaaaaa", 100, None, "now")
        conn.commit()
        # Change the anilist id
        season_ranges.upsert_season_external_id(conn, "z-aaaaaa", 999, None, "now")
        conn.commit()

        row = conn.execute(
            "SELECT external_id FROM season_external_id"
            " WHERE season_id = 'z-aaaaaa' AND service = 'anilist'"
        ).fetchone()
        assert row["external_id"] == "999"

    def test_null_id_deletes_mapping(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        conn.commit()

        season_ranges.upsert_season_external_id(conn, "z-aaaaaa", 100, None, "now")
        conn.commit()
        # Now clear the anilist_id
        season_ranges.upsert_season_external_id(conn, "z-aaaaaa", None, None, "now")
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM season_external_id WHERE season_id = 'z-aaaaaa'"
        ).fetchall()
        assert len(rows) == 0

    def test_anilist_only_no_mal(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        conn.commit()

        season_ranges.upsert_season_external_id(conn, "z-aaaaaa", 100, None, "now")
        conn.commit()

        rows = conn.execute(
            "SELECT service FROM season_external_id WHERE season_id = 'z-aaaaaa'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["service"] == "anilist"


# ---------------------------------------------------------------------------
# fill_season_ranges
# ---------------------------------------------------------------------------


class TestFillSeasonRanges:
    def test_basic_range_from_integer_absolute_numbers(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        assert row["abs_start"] == 1
        assert row["abs_end"] == 12

    def test_multi_season_ranges(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2)
        # Season 1: episodes 1-12
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        # Season 2: episodes 13-24
        for i in range(1, 13):
            _episode(conn, f"e-{i+12:06d}", "s-aaaaaa", 2, i, absolute_number=i + 12)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        s1 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'").fetchone()
        s2 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-bbbbbb'").fetchone()
        assert (s1["abs_start"], s1["abs_end"]) == (1, 12)
        assert (s2["abs_start"], s2["abs_end"]) == (13, 24)

    def test_skips_season_zero(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 0)
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 0, 1, absolute_number=1)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        assert row["abs_start"] is None
        assert row["abs_end"] is None

    def test_ignores_fractional_synthesized_absolute_numbers(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        # Real episodes with integer absolute numbers
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 1, 1, absolute_number=1)
        _episode(conn, "e-bbbbbb", "s-aaaaaa", 1, 2, absolute_number=2)
        # Synthesized specials with fractional numbers
        _episode(conn, "e-cccccc", "s-aaaaaa", 1, 3, absolute_number=2.1)
        _episode(conn, "e-dddddd", "s-aaaaaa", 1, 4, absolute_number=2.2)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        # Only integer absolute numbers counted
        assert (row["abs_start"], row["abs_end"]) == (1, 2)

    def test_skips_season_with_only_fractional_abs(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        # Only synthesized absolute numbers
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 1, 1, absolute_number=0.1)
        _episode(conn, "e-bbbbbb", "s-aaaaaa", 1, 2, absolute_number=0.2)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        assert row["abs_start"] is None
        assert row["abs_end"] is None

    def test_does_not_overwrite_existing_range(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        # Set range manually first
        conn.execute(
            "UPDATE season SET abs_start = 100, abs_end = 200 WHERE id = 'z-aaaaaa'"
        )
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 1, 1, absolute_number=1)
        _episode(conn, "e-bbbbbb", "s-aaaaaa", 1, 2, absolute_number=2)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        # Existing range preserved
        assert (row["abs_start"], row["abs_end"]) == (100, 200)

    def test_skips_season_with_no_episodes(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-aaaaaa")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        assert row["abs_start"] is None
        assert row["abs_end"] is None

    def test_skips_tv_shows(self, conn):
        """TV shows shouldn't get ranges — anime-only model."""
        _show(conn, "s-tvtvtv", tracking_space="tv")
        _season(conn, "z-tvtvtv", "s-tvtvtv", 1)
        for i in range(1, 13):
            _episode(conn, f"e-tv{i:04d}", "s-tvtvtv", 1, i, absolute_number=i)
        conn.commit()

        season_ranges.fill_season_ranges(conn, "s-tvtvtv")
        conn.commit()

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-tvtvtv'"
        ).fetchone()
        assert row["abs_start"] is None
        assert row["abs_end"] is None


# ---------------------------------------------------------------------------
# Backfill script (compute_season_ranges)
# ---------------------------------------------------------------------------


class TestComputeSeasonRanges:
    def test_basic_backfill(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100, mal_id=200)
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        conn.commit()

        report = compute_season_ranges(conn)

        assert report["ranges_set"] == 1
        assert report["external_ids_mirrored"] == 2  # anilist + mal

        row = conn.execute(
            "SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'"
        ).fetchone()
        assert (row["abs_start"], row["abs_end"]) == (1, 12)

        ext_rows = conn.execute(
            "SELECT service, external_id FROM season_external_id"
            " WHERE season_id = 'z-aaaaaa' ORDER BY service"
        ).fetchall()
        assert len(ext_rows) == 2
        assert (ext_rows[0]["service"], ext_rows[0]["external_id"]) == ("anilist", "100")
        assert (ext_rows[1]["service"], ext_rows[1]["external_id"]) == ("mal", "200")

    def test_skips_non_anime(self, conn):
        _show(conn, "s-aaaaaa", tracking_space="tv")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 1, 1, absolute_number=1)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["seasons_total"] == 0
        assert report["ranges_set"] == 0

    def test_numbering_gap_detected(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        # Absolute numbers 1, 2, 5 — gap in the middle
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 1, 1, absolute_number=1)
        _episode(conn, "e-bbbbbb", "s-aaaaaa", 1, 2, absolute_number=2)
        _episode(conn, "e-cccccc", "s-aaaaaa", 1, 3, absolute_number=5)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["ranges_set"] == 1
        # Range is [1,5] width=5 but only 3 episodes
        assert len(report["numbering_gaps"]) == 1
        assert report["numbering_gaps"][0]["range_width"] == 5
        assert report["numbering_gaps"][0]["episode_count"] == 3

    def test_overlap_detected(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2)
        # Season 1: absolute 1-12
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        # Season 2: absolute 10-20 (overlaps with season 1)
        for i in range(1, 12):
            _episode(conn, f"e-{i+12:06d}", "s-aaaaaa", 2, i, absolute_number=i + 9)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["ranges_set"] == 2
        assert any(f["type"] == "overlap" for f in report["integrity_failures"])

    def test_gap_detected(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2)
        # Season 1: absolute 1-12
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        # Season 2: absolute 15-26 (gap after 12)
        for i in range(1, 13):
            _episode(conn, f"e-{i+12:06d}", "s-aaaaaa", 2, i, absolute_number=i + 14)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["ranges_set"] == 2
        assert any(f["type"] == "gap" for f in report["integrity_failures"])

    def test_cross_boundary_detected(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2)
        # Season 1: absolute 1-12
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        # Season 2: absolute 13-24, BUT one episode has absolute_number 5
        # (belongs in season 1's range)
        _episode(conn, "e-axxxxx", "s-aaaaaa", 2, 1, absolute_number=5)
        for i in range(2, 13):
            _episode(conn, f"e-{i+12:06d}", "s-aaaaaa", 2, i, absolute_number=i + 12)
        conn.commit()

        report = compute_season_ranges(conn)
        assert any(f["type"] == "cross_boundary" for f in report["integrity_failures"])

    def test_clean_show_no_failures(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2, anilist_id=101)
        # Clean contiguous ranges
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        for i in range(1, 13):
            _episode(conn, f"e-{i+12:06d}", "s-aaaaaa", 2, i, absolute_number=i + 12)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["ranges_set"] == 2
        assert report["external_ids_mirrored"] == 2
        assert len(report["numbering_gaps"]) == 0
        assert len(report["integrity_failures"]) == 0

    def test_season_zero_skipped(self, conn):
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 0)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 1, anilist_id=100)
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 0, 1, absolute_number=1, kind="special")
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i + 1)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["ranges_skipped_season_zero"] == 1
        assert report["ranges_set"] == 1

        s0 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-aaaaaa'").fetchone()
        assert s0["abs_start"] is None

    def test_external_id_not_unique_across_seasons(self, conn):
        """Mushoku Tensei-style: one AniList entry covers multiple LCARS seasons.
        The same external_id links to both — this is the whole point of
        season_external_id not having a UNIQUE on external_id alone."""
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2, anilist_id=100)  # same anilist_id
        for i in range(1, 12):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        for i in range(1, 12):
            _episode(conn, f"e-{i+11:06d}", "s-aaaaaa", 2, i, absolute_number=i + 11)
        conn.commit()

        report = compute_season_ranges(conn)
        assert report["external_ids_mirrored"] == 2  # both anilist rows

        ext_rows = conn.execute(
            "SELECT season_id, external_id FROM season_external_id"
            " WHERE service = 'anilist' ORDER BY season_id"
        ).fetchall()
        assert len(ext_rows) == 2
        assert ext_rows[0]["external_id"] == "100"
        assert ext_rows[1]["external_id"] == "100"


# ---------------------------------------------------------------------------
# AniList width validation (check_anilist_width)
# ---------------------------------------------------------------------------


class TestCheckAnilistWidth:
    def test_mismatch_flags_wider_anilist_entry(self, conn):
        """Mushoku Tensei shape: AniList says 23 episodes but LCARS season
        has only 11 — one AniList entry spanning two LCARS seasons."""
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        _season(conn, "z-bbbbbb", "s-aaaaaa", 2, anilist_id=100)
        for i in range(1, 12):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        for i in range(1, 13):
            _episode(conn, f"e-{i+11:06d}", "s-aaaaaa", 2, i, absolute_number=i + 11)
        conn.commit()

        compute_season_ranges(conn)

        # Mock AniList: entry 100 has 23 episodes (spans both seasons)
        fake_response = {"Page": {"media": [{"id": 100, "episodes": 23}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            mismatches, not_found = check_anilist_width(conn)

        # Both seasons should be flagged: S1 width=11 vs 23, S2 width=12 vs 23
        assert len(mismatches) == 2
        assert mismatches[0]["range_width"] == 11
        assert mismatches[0]["anilist_episodes"] == 23
        assert mismatches[1]["range_width"] == 12
        assert mismatches[1]["anilist_episodes"] == 23
        assert not_found == []

    def test_matching_width_no_mismatch(self, conn):
        """Clean case: AniList episode count equals range width."""
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        conn.commit()

        compute_season_ranges(conn)

        fake_response = {"Page": {"media": [{"id": 100, "episodes": 12}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            mismatches, not_found = check_anilist_width(conn)

        assert len(mismatches) == 0
        assert not_found == []

    def test_null_anilist_episodes_skipped(self, conn):
        """AniList returns null episodes (airing show) — skip, don't flag."""
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=100)
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        conn.commit()

        compute_season_ranges(conn)

        fake_response = {"Page": {"media": [{"id": 100, "episodes": None}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            mismatches, not_found = check_anilist_width(conn)

        assert len(mismatches) == 0
        assert not_found == []

    def test_season_without_anilist_id_skipped(self, conn):
        """Seasons with no anilist_id aren't sent to AniList."""
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1)  # no anilist_id
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        conn.commit()

        compute_season_ranges(conn)

        # Should not call AniList at all — no anilist_ids to check
        with mock.patch(
            "lcars.anilist_client._graphql_request"
        ) as mock_req:
            mismatches, not_found = check_anilist_width(conn)

        mock_req.assert_not_called()
        assert len(mismatches) == 0
        assert not_found == []

    def test_not_found_anilist_id_reported(self, conn):
        """AniList returns no media for an id — dead or wrong link."""
        _show(conn, "s-aaaaaa")
        _season(conn, "z-aaaaaa", "s-aaaaaa", 1, anilist_id=99999)
        for i in range(1, 13):
            _episode(conn, f"e-{i:06d}", "s-aaaaaa", 1, i, absolute_number=i)
        conn.commit()

        compute_season_ranges(conn)

        # AniList returns empty — id 99999 not found
        fake_response = {"Page": {"media": []}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            mismatches, not_found = check_anilist_width(conn)

        assert len(mismatches) == 0  # can't compare without a count
        assert not_found == [99999]


# ---------------------------------------------------------------------------
# check_subdivision_widths (S5 — live-server width check + pending_review)
# ---------------------------------------------------------------------------


def _season_with_range_and_ext(
    conn,
    season_id,
    show_id,
    season_number,
    anilist_id,
    abs_start,
    abs_end,
):
    """Insert a season with an abs range + season_external_id anilist row."""
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id,"
        " abs_start, abs_end, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number, anilist_id, abs_start, abs_end),
    )
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES (?, 'anilist', ?, 'x')",
        (season_id, anilist_id),
    )


class TestCheckSubdivisionWidths:
    def test_no_ranged_seasons_returns_zero(self, conn):
        """No seasons with ranges → nothing to check."""
        result = season_ranges.check_subdivision_widths(conn)
        assert result == {"checked": 0, "flagged": 0}

    def test_clean_match_no_pending_review(self, conn):
        """AniList episode count == range width → no pending_review opened."""
        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, 1, 12)
        conn.commit()

        fake_response = {"Page": {"media": [{"id": 100, "episodes": 12}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            result = season_ranges.check_subdivision_widths(conn)

        assert result == {"checked": 1, "flagged": 0}
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        ).fetchone()[0]
        assert pr_count == 0

    def test_mismatch_opens_pending_review(self, conn):
        """AniList says 23 eps but range is 11 → opens pending_review."""
        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, 1, 11)
        conn.commit()

        fake_response = {"Page": {"media": [{"id": 100, "episodes": 23}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            result = season_ranges.check_subdivision_widths(conn)

        assert result == {"checked": 1, "flagged": 1}
        pr = conn.execute(
            "SELECT field, source, proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()
        assert pr is not None
        assert pr["field"] == "season_subdivision"
        assert pr["source"] == "anilist_width_check"
        import json
        chain = json.loads(pr["proposed_value_chain"])
        assert chain == ["anilist=23,range_width=11"]

    def test_idempotent_extends_not_duplicates(self, conn):
        """Second run with same mismatch → extends chain, no duplicate row."""
        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, 1, 11)
        conn.commit()

        fake_response = {"Page": {"media": [{"id": 100, "episodes": 23}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            season_ranges.check_subdivision_widths(conn)
            result2 = season_ranges.check_subdivision_widths(conn)

        assert result2["flagged"] == 1  # still flagged (extended), not zero
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()[0]
        assert pr_count == 1  # one row, not two

    def test_already_resolved_suppresses_reopen(self, conn):
        """If a human resolved the same mismatch value, don't reopen."""
        import json

        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, 1, 11)
        conn.commit()

        # Simulate a previously-resolved review with the same chain tail
        mismatch_str = "anilist=23,range_width=11"
        conn.execute(
            "INSERT INTO pending_review"
            " (id, entity_type, entity_id, field, previous_value,"
            "  proposed_value_chain, source, created_at, resolved_at)"
            " VALUES ('r-aaaaaa', 'season', 'z-aaaaaa', 'season_subdivision',"
            "  NULL, ?, 'anilist_width_check', 'x', 'x')",
            (json.dumps([mismatch_str]),),
        )
        conn.commit()

        fake_response = {"Page": {"media": [{"id": 100, "episodes": 23}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            result = season_ranges.check_subdivision_widths(conn)

        assert result == {"checked": 1, "flagged": 0}
        # No new open row
        open_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()[0]
        assert open_count == 0

    def test_airing_null_episodes_skipped(self, conn):
        """AniList returns null episodes (airing) → not counted, not flagged."""
        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, 1, 12)
        conn.commit()

        # AniList says episodes: null (still airing)
        fake_response = {"Page": {"media": [{"id": 100, "episodes": None}]}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            result = season_ranges.check_subdivision_widths(conn)

        assert result == {"checked": 0, "flagged": 0}

    def test_not_found_in_anilist_skipped(self, conn):
        """AniList returns no entry for the id → skip (dead/wrong link)."""
        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 99999, 1, 12)
        conn.commit()

        fake_response = {"Page": {"media": []}}
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            result = season_ranges.check_subdivision_widths(conn)

        assert result == {"checked": 0, "flagged": 0}

    def test_multiple_seasons_mixed(self, conn):
        """Two seasons: one clean, one mismatch → checked=2, flagged=1."""
        _show(conn, "s-aaaaaa")
        _season_with_range_and_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, 1, 12)
        _season_with_range_and_ext(conn, "z-bbbbbb", "s-aaaaaa", 2, 101, 13, 23)
        conn.commit()

        fake_response = {
            "Page": {
                "media": [
                    {"id": 100, "episodes": 12},   # clean
                    {"id": 101, "episodes": 13},   # mismatch: range=11, anilist=13
                ]
            }
        }
        with mock.patch(
            "lcars.anilist_client._graphql_request", return_value=fake_response
        ):
            result = season_ranges.check_subdivision_widths(conn)

        assert result == {"checked": 2, "flagged": 1}
        pr_s1 = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        ).fetchone()[0]
        assert pr_s1 == 0

        pr_s2 = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-bbbbbb'"
        ).fetchone()[0]
        assert pr_s2 == 1


# ---------------------------------------------------------------------------
# ensure_all_season_rows + backfill_episode_season_id (Step 2)
# ---------------------------------------------------------------------------


class TestEnsureAllSeasonRows:
    def test_creates_tv_season_rows_without_review(self, conn):
        """TV shows get season rows directly, no pending_review noise."""
        _show(conn, "s-tv0001", "NCIS", tracking_space="tv")
        _episode(conn, "e-aa0001", "s-tv0001", 1, 1)
        _episode(conn, "e-aa0002", "s-tv0001", 1, 2)
        _episode(conn, "e-aa0003", "s-tv0001", 2, 1)
        conn.commit()

        created = season_ranges.ensure_all_season_rows(conn)
        assert created == 2  # season 1 and 2

        seasons = conn.execute(
            "SELECT season_number, source, matched FROM season"
            " WHERE show_id = 's-tv0001' ORDER BY season_number"
        ).fetchall()
        assert len(seasons) == 2
        assert seasons[0]["season_number"] == 1
        assert seasons[0]["source"] == "unmatched"
        assert seasons[0]["matched"] == 0
        assert seasons[1]["season_number"] == 2

        # No pending_review rows for TV seasons
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season'"
        ).fetchone()[0]
        assert pr_count == 0

    def test_skips_season_zero(self, conn):
        """Season 0 (specials) should not get a season row."""
        _show(conn, "s-tv0002", "House", tracking_space="tv")
        _episode(conn, "e-bb0001", "s-tv0002", 0, 1, kind="special")
        _episode(conn, "e-bb0002", "s-tv0002", 1, 1)
        conn.commit()

        created = season_ranges.ensure_all_season_rows(conn)
        assert created == 1  # only season 1

        seasons = conn.execute(
            "SELECT season_number FROM season WHERE show_id = 's-tv0002'"
        ).fetchall()
        assert len(seasons) == 1
        assert seasons[0]["season_number"] == 1

    def test_idempotent(self, conn):
        """Running twice creates nothing on the second call."""
        _show(conn, "s-tv0003", "Dexter", tracking_space="tv")
        _episode(conn, "e-cc0001", "s-tv0003", 1, 1)
        conn.commit()

        first = season_ranges.ensure_all_season_rows(conn)
        assert first == 1
        second = season_ranges.ensure_all_season_rows(conn)
        assert second == 0

    def test_does_not_touch_existing_seasons(self, conn):
        """Pre-existing season rows are untouched."""
        _show(conn, "s-tv0004", "CSI", tracking_space="tv")
        _season(conn, "z-exists", "s-tv0004", 1)
        _episode(conn, "e-dd0001", "s-tv0004", 1, 1)
        _episode(conn, "e-dd0002", "s-tv0004", 2, 1)
        conn.commit()

        created = season_ranges.ensure_all_season_rows(conn)
        assert created == 1  # only season 2

        # Original season row untouched
        original = conn.execute(
            "SELECT id FROM season WHERE show_id = 's-tv0004' AND season_number = 1"
        ).fetchone()
        assert original["id"] == "z-exists"


    def test_anime_uses_reconcile_season(self, conn):
        """Anime shows go through reconcile_season, not direct insert."""
        _show(conn, "s-an0001", "Naruto", tracking_space="anime")
        _episode(conn, "e-na0001", "s-an0001", 1, 1)
        conn.commit()

        with mock.patch("lcars.season_mapping.reconcile_season") as mock_rec:
            # reconcile_season should create the row — simulate that
            def fake_reconcile(c, show_id, season_number):
                sid = "z-fake01"
                c.execute(
                    "INSERT INTO season"
                    " (id, show_id, season_number, status, source, matched,"
                    "  manual_override, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'planned', 'fribb', 1, 0, 'x', 'x')",
                    (sid, show_id, season_number),
                )
                c.commit()

            mock_rec.side_effect = fake_reconcile
            created = season_ranges.ensure_all_season_rows(conn)

        assert created == 1
        mock_rec.assert_called_once()
        row = conn.execute(
            "SELECT source FROM season WHERE show_id = 's-an0001' AND season_number = 1"
        ).fetchone()
        assert row["source"] == "fribb"

    def test_anime_fallback_guards_double_insert(self, conn):
        """If reconcile_season commits a row then raises, fallback skips insert."""
        _show(conn, "s-an0002", "Bleach", tracking_space="anime")
        _episode(conn, "e-bl0001", "s-an0002", 1, 1)
        conn.commit()

        with mock.patch("lcars.season_mapping.reconcile_season") as mock_rec:
            # Simulate: reconcile commits the season row, then raises
            def fake_reconcile_then_fail(c, show_id, season_number):
                sid = "z-fake02"
                c.execute(
                    "INSERT INTO season"
                    " (id, show_id, season_number, status, source, matched,"
                    "  manual_override, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'planned', 'unmatched', 0, 0, 'x', 'x')",
                    (sid, show_id, season_number),
                )
                c.commit()
                raise RuntimeError("pending_review failed")

            mock_rec.side_effect = fake_reconcile_then_fail
            created = season_ranges.ensure_all_season_rows(conn)

        assert created == 1
        # Only one season row, not two
        count = conn.execute(
            "SELECT COUNT(*) FROM season WHERE show_id = 's-an0002' AND season_number = 1"
        ).fetchone()[0]
        assert count == 1


class TestBackfillEpisodeSeasonId:
    def test_links_episodes_to_seasons(self, conn):
        """Episodes with NULL season_id get linked to their season row."""
        _show(conn, "s-tv0010", "Breaking Bad", tracking_space="tv")
        _season(conn, "z-bb0001", "s-tv0010", 1)
        _season(conn, "z-bb0002", "s-tv0010", 2)
        _episode(conn, "e-ee0001", "s-tv0010", 1, 1)
        _episode(conn, "e-ee0002", "s-tv0010", 1, 2)
        _episode(conn, "e-ee0003", "s-tv0010", 2, 1)
        conn.commit()

        updated = season_ranges.backfill_episode_season_id(conn)
        assert updated == 3

        e1 = conn.execute("SELECT season_id FROM episode WHERE id = 'e-ee0001'").fetchone()
        assert e1["season_id"] == "z-bb0001"
        e3 = conn.execute("SELECT season_id FROM episode WHERE id = 'e-ee0003'").fetchone()
        assert e3["season_id"] == "z-bb0002"

    def test_null_only(self, conn):
        """Never overwrites an existing season_id."""
        _show(conn, "s-tv0011", "Fargo", tracking_space="tv")
        _season(conn, "z-fg0001", "s-tv0011", 1)
        _season(conn, "z-fg0002", "s-tv0011", 2)
        # Episode already linked to season 2 (maybe manually corrected)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, kind,"
            " state, created_at, updated_at)"
            " VALUES ('e-ff0001', 's-tv0011', 1, 'z-fg0002', 1, 'regular',"
            " 'unwatched', 'x', 'x')"
        )
        conn.commit()

        updated = season_ranges.backfill_episode_season_id(conn)
        assert updated == 0  # nothing changed

        e = conn.execute("SELECT season_id FROM episode WHERE id = 'e-ff0001'").fetchone()
        assert e["season_id"] == "z-fg0002"  # still season 2, not overwritten

    def test_skips_season_zero(self, conn):
        """Specials (season 0) stay with NULL season_id."""
        _show(conn, "s-tv0012", "Sherlock", tracking_space="tv")
        _season(conn, "z-sh0001", "s-tv0012", 1)
        _episode(conn, "e-gg0001", "s-tv0012", 0, 1, kind="special")
        _episode(conn, "e-gg0002", "s-tv0012", 1, 1)
        conn.commit()

        updated = season_ranges.backfill_episode_season_id(conn)
        assert updated == 1  # only the season 1 episode

        special = conn.execute("SELECT season_id FROM episode WHERE id = 'e-gg0001'").fetchone()
        assert special["season_id"] is None

    def test_idempotent(self, conn):
        """Running twice does nothing the second time."""
        _show(conn, "s-tv0013", "Suits", tracking_space="tv")
        _season(conn, "z-su0001", "s-tv0013", 1)
        _episode(conn, "e-hh0001", "s-tv0013", 1, 1)
        conn.commit()

        first = season_ranges.backfill_episode_season_id(conn)
        assert first == 1
        second = season_ranges.backfill_episode_season_id(conn)
        assert second == 0

    def test_combined_flow(self, conn):
        """ensure_all_season_rows then backfill_episode_season_id — full flow."""
        _show(conn, "s-tv0014", "The Wire", tracking_space="tv")
        _episode(conn, "e-ii0001", "s-tv0014", 1, 1)
        _episode(conn, "e-ii0002", "s-tv0014", 1, 2)
        _episode(conn, "e-ii0003", "s-tv0014", 2, 1)
        _episode(conn, "e-ii0004", "s-tv0014", 0, 1, kind="special")
        conn.commit()

        season_rows = season_ranges.ensure_all_season_rows(conn)
        assert season_rows == 2

        linked = season_ranges.backfill_episode_season_id(conn)
        assert linked == 3  # 2 from season 1 + 1 from season 2

        # Special stays NULL
        sp = conn.execute("SELECT season_id FROM episode WHERE id = 'e-ii0004'").fetchone()
        assert sp["season_id"] is None

        # Season 1 episodes are linked
        s1_id = conn.execute(
            "SELECT id FROM season WHERE show_id = 's-tv0014' AND season_number = 1"
        ).fetchone()["id"]
        for eid in ("e-ii0001", "e-ii0002"):
            e = conn.execute("SELECT season_id FROM episode WHERE id = ?", (eid,)).fetchone()
            assert e["season_id"] == s1_id


# ---------------------------------------------------------------------------
# seed_episode_external_ids (Step 3)
# ---------------------------------------------------------------------------


def _anidb_mapping(conn, episode_id, anidb_anime_id, anidb_season, anidb_epno):
    conn.execute(
        "INSERT INTO episode_anidb_mapping"
        " (episode_id, anidb_anime_id, anidb_season, anidb_epno, confidence, created_at)"
        " VALUES (?, ?, ?, ?, 'auto', 'x')",
        (episode_id, anidb_anime_id, anidb_season, anidb_epno),
    )


def _season_ext_id(conn, season_id, service, external_id):
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES (?, ?, ?, 'x')",
        (season_id, service, str(external_id)),
    )


class TestSeedEpisodeExternalIds:
    def test_anidb_from_mapping(self, conn):
        _show(conn, "s-aa0001", "Naruto", tracking_space="anime")
        _season(conn, "z-aa0001", "s-aa0001", 1)
        _episode(conn, "e-na0001", "s-aa0001", 1, 1)
        _episode(conn, "e-na0002", "s-aa0001", 1, 2)
        _anidb_mapping(conn, "e-na0001", 20, 1, 1)
        _anidb_mapping(conn, "e-na0002", 20, 1, 2)
        conn.commit()

        result = season_ranges.seed_episode_external_ids(conn)
        assert result["anidb"] == 2

        rows = conn.execute(
            "SELECT episode_id, external_id, season_number, episode_number"
            " FROM episode_external_id WHERE service = 'anidb'"
            " ORDER BY episode_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["external_id"] == "20:1"
        assert rows[0]["season_number"] == 1
        assert rows[0]["episode_number"] == 1

    def test_tvdb_from_sonarr(self, conn):
        _show(conn, "s-tv0020", "House", tracking_space="tv")
        _season(conn, "z-tv0020", "s-tv0020", 1)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind,"
            " sonarr_season, sonarr_episode, state, created_at, updated_at)"
            " VALUES ('e-hh0010', 's-tv0020', 1, 1, 'regular',"
            " 1, 1, 'unwatched', 'x', 'x')"
        )
        conn.commit()

        result = season_ranges.seed_episode_external_ids(conn)
        assert result["tvdb"] == 1

        row = conn.execute(
            "SELECT external_id, season_number, episode_number"
            " FROM episode_external_id"
            " WHERE episode_id = 'e-hh0010' AND service = 'tvdb'"
        ).fetchone()
        assert row["external_id"] == "1:1"
        assert row["season_number"] == 1
        assert row["episode_number"] == 1

    def test_anilist_mal_from_season_ext_id(self, conn):
        _show(conn, "s-an0010", "Bleach", tracking_space="anime")
        _season(conn, "z-an0010", "s-an0010", 1)
        _season_ext_id(conn, "z-an0010", "anilist", 100)
        _season_ext_id(conn, "z-an0010", "mal", 200)
        # Link episodes to season
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, kind,"
            " state, created_at, updated_at)"
            " VALUES ('e-bl0010', 's-an0010', 1, 'z-an0010', 1, 'regular',"
            " 'unwatched', 'x', 'x')"
        )
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, kind,"
            " state, created_at, updated_at)"
            " VALUES ('e-bl0011', 's-an0010', 1, 'z-an0010', 2, 'regular',"
            " 'unwatched', 'x', 'x')"
        )
        conn.commit()

        result = season_ranges.seed_episode_external_ids(conn)
        assert result["anilist"] == 2
        assert result["mal"] == 2

        al_rows = conn.execute(
            "SELECT episode_id, external_id, episode_number"
            " FROM episode_external_id WHERE service = 'anilist'"
            " ORDER BY episode_id"
        ).fetchall()
        assert len(al_rows) == 2
        assert al_rows[0]["external_id"] == "100:1"
        assert al_rows[0]["episode_number"] == 1
        assert al_rows[1]["external_id"] == "100:2"
        assert al_rows[1]["episode_number"] == 2

    def test_idempotent(self, conn):
        _show(conn, "s-tv0021", "CSI", tracking_space="tv")
        _season(conn, "z-tv0021", "s-tv0021", 1)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind,"
            " sonarr_season, sonarr_episode, state, created_at, updated_at)"
            " VALUES ('e-cs0001', 's-tv0021', 1, 1, 'regular',"
            " 1, 1, 'unwatched', 'x', 'x')"
        )
        conn.commit()

        first = season_ranges.seed_episode_external_ids(conn)
        assert first["tvdb"] == 1
        second = season_ranges.seed_episode_external_ids(conn)
        assert second["tvdb"] == 0

    def test_skips_null_anidb_epno(self, conn):
        _show(conn, "s-an0011", "Test", tracking_space="anime")
        _season(conn, "z-an0011", "s-an0011", 1)
        _episode(conn, "e-te0001", "s-an0011", 1, 1)
        conn.execute(
            "INSERT INTO episode_anidb_mapping"
            " (episode_id, anidb_anime_id, anidb_season, anidb_epno, confidence, created_at)"
            " VALUES ('e-te0001', 20, 1, NULL, 'auto', 'x')"
        )
        conn.commit()

        result = season_ranges.seed_episode_external_ids(conn)
        assert result["anidb"] == 0


# ---------------------------------------------------------------------------
# backfill_season_names (Step 4)
# ---------------------------------------------------------------------------


def _ensure_anidb_anime(conn, anidb_id):
    """Insert anidb_anime parent row if missing (FK target for anidb_title)."""
    if not conn.execute(
        "SELECT 1 FROM anidb_anime WHERE anidb_id = ?", (anidb_id,)
    ).fetchone():
        conn.execute(
            "INSERT INTO anidb_anime (anidb_id, main_title, fetched_at)"
            " VALUES (?, 'stub', 'x')",
            (anidb_id,),
        )


def _anidb_title(conn, anidb_id, title, lang="en", title_type=1):
    _ensure_anidb_anime(conn, anidb_id)
    conn.execute(
        "INSERT INTO anidb_title (anidb_id, title, lang, title_type)"
        " VALUES (?, ?, ?, ?)",
        (anidb_id, title, lang, title_type),
    )


class TestBackfillSeasonNames:
    def test_fills_from_anidb_title(self, conn):
        _show(conn, "s-an0020", "KnS", tracking_space="anime")
        _season(conn, "z-an0020", "s-an0020", 1)
        _season_ext_id(conn, "z-an0020", "anilist", 100)
        # Link episode to season and to AniDB mapping
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, kind,"
            " state, created_at, updated_at)"
            " VALUES ('e-kn0001', 's-an0020', 1, 'z-an0020', 1, 'regular',"
            " 'unwatched', 'x', 'x')"
        )
        _anidb_mapping(conn, "e-kn0001", 9892, 1, 1)
        _anidb_title(conn, 9892, "Knights of Sidonia")
        conn.commit()

        filled = season_ranges.backfill_season_names(conn)
        assert filled == 1

        row = conn.execute(
            "SELECT name FROM season_external_id"
            " WHERE season_id = 'z-an0020' AND service = 'anilist'"
        ).fetchone()
        assert row["name"] == "Knights of Sidonia"

    def test_falls_back_to_romaji(self, conn):
        _show(conn, "s-an0021", "Test", tracking_space="anime")
        _season(conn, "z-an0021", "s-an0021", 1)
        _season_ext_id(conn, "z-an0021", "anilist", 101)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, season_id, episode, kind,"
            " state, created_at, updated_at)"
            " VALUES ('e-rm0001', 's-an0021', 1, 'z-an0021', 1, 'regular',"
            " 'unwatched', 'x', 'x')"
        )
        _anidb_mapping(conn, "e-rm0001", 555, 1, 1)
        # Only romaji title, no English
        _anidb_title(conn, 555, "Sidonia no Kishi", lang="x-jat")
        conn.commit()

        filled = season_ranges.backfill_season_names(conn)
        assert filled == 1

        row = conn.execute(
            "SELECT name FROM season_external_id"
            " WHERE season_id = 'z-an0021' AND service = 'anilist'"
        ).fetchone()
        assert row["name"] == "Sidonia no Kishi"

    def test_single_season_uses_show_title(self, conn):
        _show(conn, "s-an0022", "One Piece", tracking_space="anime")
        _season(conn, "z-an0022", "s-an0022", 1)
        _season_ext_id(conn, "z-an0022", "anilist", 102)
        # No AniDB mapping, single season
        conn.commit()

        filled = season_ranges.backfill_season_names(conn)
        assert filled == 1

        row = conn.execute(
            "SELECT name FROM season_external_id"
            " WHERE season_id = 'z-an0022' AND service = 'anilist'"
        ).fetchone()
        assert row["name"] == "One Piece"

    def test_null_only(self, conn):
        _show(conn, "s-an0023", "Test", tracking_space="anime")
        _season(conn, "z-an0023", "s-an0023", 1)
        conn.execute(
            "INSERT INTO season_external_id"
            " (season_id, service, external_id, name, created_at)"
            " VALUES ('z-an0023', 'anilist', '103', 'Custom Name', 'x')"
        )
        conn.commit()

        filled = season_ranges.backfill_season_names(conn)
        assert filled == 0

        row = conn.execute(
            "SELECT name FROM season_external_id"
            " WHERE season_id = 'z-an0023' AND service = 'anilist'"
        ).fetchone()
        assert row["name"] == "Custom Name"

    def test_idempotent(self, conn):
        _show(conn, "s-an0024", "FMA", tracking_space="anime")
        _season(conn, "z-an0024", "s-an0024", 1)
        _season_ext_id(conn, "z-an0024", "anilist", 104)
        conn.commit()

        first = season_ranges.backfill_season_names(conn)
        assert first == 1
        second = season_ranges.backfill_season_names(conn)
        assert second == 0


# ---------------------------------------------------------------------------
# fill_season_ranges_bulk (Step 5)
# ---------------------------------------------------------------------------


class TestFillSeasonRangesBulk:
    def test_anime_from_absolute_number(self, conn):
        _show(conn, "s-an0030", "Naruto", tracking_space="anime")
        _season(conn, "z-an0030", "s-an0030", 1)
        _season(conn, "z-an0031", "s-an0030", 2)
        for i in range(1, 13):
            _episode(conn, f"e-nr{i:04d}", "s-an0030", 1, i, absolute_number=i)
        for i in range(1, 13):
            _episode(conn, f"e-ns{i:04d}", "s-an0030", 2, i, absolute_number=i + 12)
        conn.commit()

        updated = season_ranges.fill_season_ranges_bulk(conn)
        assert updated == 2

        s1 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-an0030'").fetchone()
        assert (s1["abs_start"], s1["abs_end"]) == (1, 12)
        s2 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-an0031'").fetchone()
        assert (s2["abs_start"], s2["abs_end"]) == (13, 24)

    def test_tv_from_episode_counts(self, conn):
        _show(conn, "s-tv0030", "House", tracking_space="tv")
        _season(conn, "z-tv0030", "s-tv0030", 1)
        _season(conn, "z-tv0031", "s-tv0030", 2)
        for i in range(1, 23):
            _episode(conn, f"e-h1{i:04d}", "s-tv0030", 1, i)
        for i in range(1, 14):
            _episode(conn, f"e-h2{i:04d}", "s-tv0030", 2, i)
        conn.commit()

        updated = season_ranges.fill_season_ranges_bulk(conn)
        assert updated == 2

        s1 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-tv0030'").fetchone()
        assert (s1["abs_start"], s1["abs_end"]) == (1, 22)
        s2 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-tv0031'").fetchone()
        assert (s2["abs_start"], s2["abs_end"]) == (23, 35)

    def test_tv_respects_existing_ranges(self, conn):
        _show(conn, "s-tv0031", "CSI", tracking_space="tv")
        _season(conn, "z-tv0032", "s-tv0031", 1)
        _season(conn, "z-tv0033", "s-tv0031", 2)
        # Season 1 already has range set
        conn.execute(
            "UPDATE season SET abs_start = 1, abs_end = 24 WHERE id = 'z-tv0032'"
        )
        for i in range(1, 25):
            _episode(conn, f"e-c1{i:04d}", "s-tv0031", 1, i)
        for i in range(1, 13):
            _episode(conn, f"e-c2{i:04d}", "s-tv0031", 2, i)
        conn.commit()

        updated = season_ranges.fill_season_ranges_bulk(conn)
        assert updated == 1  # only season 2

        s2 = conn.execute("SELECT abs_start, abs_end FROM season WHERE id = 'z-tv0033'").fetchone()
        assert (s2["abs_start"], s2["abs_end"]) == (25, 36)

    def test_skips_season_zero(self, conn):
        _show(conn, "s-tv0032", "Dexter", tracking_space="tv")
        _season(conn, "z-tv0034", "s-tv0032", 0)
        _episode(conn, "e-dx0001", "s-tv0032", 0, 1, kind="special")
        conn.commit()

        updated = season_ranges.fill_season_ranges_bulk(conn)
        assert updated == 0

    def test_idempotent(self, conn):
        _show(conn, "s-tv0033", "Suits", tracking_space="tv")
        _season(conn, "z-tv0035", "s-tv0033", 1)
        for i in range(1, 11):
            _episode(conn, f"e-su{i:04d}", "s-tv0033", 1, i)
        conn.commit()

        first = season_ranges.fill_season_ranges_bulk(conn)
        assert first == 1
        second = season_ranges.fill_season_ranges_bulk(conn)
        assert second == 0


# ---------------------------------------------------------------------------
# W2 — inherit_season_status
# ---------------------------------------------------------------------------

class TestInheritSeasonStatus:

    def test_watching_show_gets_planned(self, conn):
        _show(conn, "s-watc01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'watching' WHERE id = 's-watc01'")
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-watc01") == "planned"

    def test_dropped_show_gets_dropped(self, conn):
        _show(conn, "s-drop01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'dropped' WHERE id = 's-drop01'")
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-drop01") == "dropped"

    def test_paused_show_gets_paused(self, conn):
        _show(conn, "s-paus01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'paused' WHERE id = 's-paus01'")
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-paus01") == "paused"

    def test_planned_show_gets_planned(self, conn):
        _show(conn, "s-plan01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'planned' WHERE id = 's-plan01'")
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-plan01") == "planned"

    def test_completed_show_gets_planned(self, conn):
        _show(conn, "s-comp01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'completed' WHERE id = 's-comp01'")
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-comp01") == "planned"

    def test_dropped_but_sonarr_followed_gets_planned(self, conn):
        _show(conn, "s-drps01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'dropped' WHERE id = 's-drps01'")
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES ('s-drps01', 'sonarr', 'test-slug', 'http://sonarr/series/test', 'x')"
        )
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-drps01") == "planned"

    def test_paused_but_radarr_followed_gets_planned(self, conn):
        _show(conn, "s-psrd01", tracking_space="anime")
        conn.execute("UPDATE show SET status = 'paused' WHERE id = 's-psrd01'")
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES ('s-psrd01', 'radarr', 'test-slug', 'http://radarr/movie/test', 'x')"
        )
        conn.commit()
        assert season_ranges.inherit_season_status(conn, "s-psrd01") == "planned"

    def test_missing_show_defaults_to_planned(self, conn):
        assert season_ranges.inherit_season_status(conn, "s-noex01") == "planned"
