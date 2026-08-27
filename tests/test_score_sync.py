"""Score reverse-sync (2026-08-27) — check_anilist_score_drift unit tests.

Same real-migrated-SQLite-DB approach as test_season_ranges.py.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from lcars import config, score_sync


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "score_sync_test.db"
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
    config.get_current().anilist_access_token = "test-token"
    yield
    config.set_current(config.Config())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _show(conn, show_id="s-aaaaaa", score=None):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji,"
        " primary_title, status, tracked, score, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'romaji', 'watching', 1, ?, 'x', 'x')",
        (show_id, show_id, score),
    )
    return show_id


def _season_with_ext(conn, season_id, show_id, season_number, anilist_id, score=None):
    """Insert a season with an AniList season_external_id row."""
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id,"
        " score, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number, anilist_id, score),
    )
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES (?, 'anilist', ?, 'x')",
        (season_id, anilist_id),
    )


def _fake_list(entries):
    """Return a mock target for fetch_my_anime_list with given entries."""
    return mock.patch(
        "lcars.anilist_client.fetch_my_anime_list",
        return_value=entries,
    )


def _al(anilist_id, score):
    """Shorthand for an AniList list entry dict."""
    return {"anilist_id": anilist_id, "score": score}


# ---------------------------------------------------------------------------
# No token
# ---------------------------------------------------------------------------


class TestNoToken:
    def test_no_token_returns_zero(self, conn):
        """No AniList token → immediate zero return, no AniList call."""
        config.get_current().anilist_access_token = None
        with mock.patch("lcars.anilist_client.fetch_my_anime_list") as m:
            result = score_sync.check_anilist_score_drift(conn)
        m.assert_not_called()
        assert result == {"checked": 0, "flagged": 0}


# ---------------------------------------------------------------------------
# Clean / no drift
# ---------------------------------------------------------------------------


class TestClean:
    def test_consistent_season_score_no_flag(self, conn):
        """AniList score matches what LCARS would have pushed → no flag."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        # LCARS 17.0 × 5 = 85; AniList returns 85 → consistent
        with _fake_list([_al(100, 85)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review WHERE entity_type = 'season'"
        ).fetchone()[0]
        assert pr_count == 0

    def test_show_score_fallback_consistent(self, conn):
        """Season has no score but show's score × 5 matches AniList → no flag."""
        _show(conn, "s-aaaaaa", score=16.0)  # show score
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=None)  # no season score
        conn.commit()

        # show score 16.0 × 5 = 80; AniList returns 80
        with _fake_list([_al(100, 80)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}

    def test_quarter_point_rounding_no_false_positive(self, conn):
        """LCARS 17.25 × 5 = 86.25 → expected 86 (rounded).
        AniList stores 86 (integer snap) → no false positive."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.25)
        conn.commit()

        # round(17.25 * 5) = round(86.25) = 86 (banker's)
        with _fake_list([_al(100, 86)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}

    def test_unscored_externally_skipped(self, conn):
        """AniList score = 0 (unscored) → skip, no flag even if LCARS has a score."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        with _fake_list([_al(100, 0)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 0, "flagged": 0}

    def test_not_on_anilist_list_skipped(self, conn):
        """Season has AniList link but isn't in the fetched list → skip."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        with _fake_list([]):  # no entries
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 0, "flagged": 0}


# ---------------------------------------------------------------------------
# Drift detected
# ---------------------------------------------------------------------------


class TestDrift:
    def test_drift_opens_pending_review(self, conn):
        """AniList score changed externally → opens pending_review with
        LCARS-scale proposed value in proposed_value_chain."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        # LCARS would push 85; AniList now shows 90 (user changed it there)
        with _fake_list([_al(100, 90)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        pr = conn.execute(
            "SELECT field, source, previous_value, proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()
        assert pr is not None
        assert pr["field"] == "score"
        assert pr["source"] == "anilist_score_drift"
        assert pr["previous_value"] == "17.0"  # was the LCARS season score
        chain = json.loads(pr["proposed_value_chain"])
        assert chain == ["18.0"]  # 90 / 5 = 18.0

    def test_lcars_no_score_anilist_has_score(self, conn):
        """LCARS has no score, AniList does → flag as inbound score."""
        _show(conn, "s-aaaaaa", score=None)
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=None)
        conn.commit()

        with _fake_list([_al(100, 75)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        pr = conn.execute(
            "SELECT previous_value, proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()
        assert pr is not None
        assert pr["previous_value"] is None  # no prior LCARS score
        chain = json.loads(pr["proposed_value_chain"])
        assert chain == ["15.0"]  # 75 / 5 = 15.0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_extends_not_duplicates(self, conn):
        """Same drift on back-to-back calls → chain extended, no duplicate row."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        with _fake_list([_al(100, 90)]):
            score_sync.check_anilist_score_drift(conn)
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        ).fetchone()[0]
        assert pr_count == 1
        pr = conn.execute(
            "SELECT proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        ).fetchone()
        chain = json.loads(pr["proposed_value_chain"])
        assert chain == ["18.0", "18.0"]

    def test_already_resolved_suppresses_reopen(self, conn):
        """Human resolved the exact proposed value → no re-open on next tick."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        with _fake_list([_al(100, 90)]):
            score_sync.check_anilist_score_drift(conn)

        # Simulate human resolving the review
        conn.execute(
            "UPDATE pending_review SET resolved_at = 'now'"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        )
        conn.commit()

        # Next tick: same drift, same proposed value → should not re-open
        with _fake_list([_al(100, 90)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}
        open_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()[0]
        assert open_count == 0

    def test_new_value_after_resolve_reopens(self, conn):
        """A genuinely different external score after resolving reopens the review."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        conn.commit()

        # First drift: AniList = 90
        with _fake_list([_al(100, 90)]):
            score_sync.check_anilist_score_drift(conn)
        conn.execute(
            "UPDATE pending_review SET resolved_at = 'now'"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        )
        conn.commit()

        # Second drift: AniList now = 95 (different value)
        with _fake_list([_al(100, 95)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        open_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()[0]
        assert open_count == 1


# ---------------------------------------------------------------------------
# Multiple seasons
# ---------------------------------------------------------------------------


class TestMultiple:
    def test_mixed_clean_and_drift(self, conn):
        """Two seasons: one clean, one drifted → only the drifted one flagged."""
        _show(conn, "s-aaaaaa")
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=17.0)
        _season_with_ext(conn, "z-bbbbbb", "s-aaaaaa", 2, 101, score=16.0)
        conn.commit()

        # season z-aaaaaa: push=85, AniList=85 → clean
        # season z-bbbbbb: push=80, AniList=90 → drift
        with _fake_list([_al(100, 85), _al(101, 90)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 2, "flagged": 1}
        flagged_id = conn.execute(
            "SELECT entity_id FROM pending_review WHERE resolved_at IS NULL"
        ).fetchone()
        assert flagged_id["entity_id"] == "z-bbbbbb"
