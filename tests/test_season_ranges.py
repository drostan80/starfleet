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
        " primary_title, status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, ?, 'romaji', 'watching', 1, 'x', 'x')",
        (show_id, tracking_space, title),
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
        assert (rows[0]["service"], rows[0]["external_id"]) == ("anilist", 100)
        assert (rows[1]["service"], rows[1]["external_id"]) == ("mal", 200)

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
        assert row["external_id"] == 999

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
        assert (ext_rows[0]["service"], ext_rows[0]["external_id"]) == ("anilist", 100)
        assert (ext_rows[1]["service"], ext_rows[1]["external_id"]) == ("mal", 200)

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
        assert ext_rows[0]["external_id"] == 100
        assert ext_rows[1]["external_id"] == 100


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
