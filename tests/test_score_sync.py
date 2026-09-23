"""Score reverse-sync — check_anilist_score_drift and check_mal_score_drift
unit tests (2026-08-27).

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

    def test_show_score_fallback_never_flagged_as_drift(self, conn):
        """2026-09-20 fix: a season with no score of its own is skipped
        entirely when a show-level score exists — never compared, even
        when AniList's real per-season score disagrees with the show-wide
        fallback. This was the actual source of the ~238 noise reviews:
        a multi-season show scored once at the show level but rated
        genuinely differently per season on AniList used to flag every
        season the show-level score didn't happen to match."""
        _show(conn, "s-aaaaaa", score=16.0)  # show score
        _season_with_ext(conn, "z-aaaaaa", "s-aaaaaa", 1, 100, score=None)  # no season score
        conn.commit()

        # show score 16.0 × 5 = 80; AniList genuinely disagrees (this
        # season is really scored 90 there) — must not be flagged.
        with _fake_list([_al(100, 90)]):
            result = score_sync.check_anilist_score_drift(conn)

        assert result == {"checked": 0, "flagged": 0}
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review WHERE entity_type = 'season'"
        ).fetchone()[0]
        assert pr_count == 0

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


# ===========================================================================
# check_mal_score_drift
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers (MAL-specific)
# ---------------------------------------------------------------------------


def _season_with_mal(conn, season_id, show_id, season_number, mal_id, score=None):
    """Insert a season with mal_id set directly on the season row."""
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, mal_id,"
        " score, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number, mal_id, score),
    )


def _fake_mal_list(entries):
    """Return a mock target for mal_client.fetch_my_list with given entries."""
    return mock.patch(
        "lcars.mal_client.fetch_my_list",
        return_value=entries,
    )


def _mal(mal_id, score):
    """Shorthand for a MAL list entry dict."""
    return {"mal_id": mal_id, "score": score, "status": "watching", "num_watched_episodes": 0}


# ---------------------------------------------------------------------------
# No MAL token
# ---------------------------------------------------------------------------


class TestMalNoToken:
    def test_no_token_returns_zero(self, conn):
        """No MAL token → immediate zero return, no MAL call."""
        config.get_current().mal_access_token = None
        with mock.patch("lcars.mal_client.fetch_my_list") as m:
            result = score_sync.check_mal_score_drift(conn)
        m.assert_not_called()
        assert result == {"checked": 0, "flagged": 0}

    def test_with_token_calls_fetch(self, conn):
        """MAL token present → fetch_my_list is called."""
        config.get_current().mal_access_token = "mal-token"
        with _fake_mal_list([]) as m:
            score_sync.check_mal_score_drift(conn)
        m.assert_called_once_with("mal-token")


# ---------------------------------------------------------------------------
# Clean / no drift (MAL)
# ---------------------------------------------------------------------------


class TestMalClean:
    def setup_method(self, _):
        # Ensure MAL token is set for each test in this class.
        config.get_current().mal_access_token = "mal-token"

    def test_consistent_season_score_no_flag(self, conn):
        """MAL score matches round(effective_lcars / 2) → no flag."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        # LCARS 16.0 / 2 = 8 → expected_mal = 8; MAL returns 8 → consistent
        with _fake_mal_list([_mal(200, 8)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review WHERE entity_type = 'season'"
        ).fetchone()[0]
        assert pr_count == 0

    def test_show_score_fallback_never_flagged_as_drift(self, conn):
        """2026-09-20 fix: a season with no score of its own is skipped
        entirely when a show-level score exists — same reasoning as
        check_anilist_score_drift's own fix/test."""
        _show(conn, "s-aaaaaa", score=14.0)
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=None)
        conn.commit()

        # show score 14.0 / 2 = 7; MAL genuinely disagrees (this season is
        # really scored 9 there) — must not be flagged.
        with _fake_mal_list([_mal(200, 9)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 0, "flagged": 0}
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review WHERE entity_type = 'season'"
        ).fetchone()[0]
        assert pr_count == 0

    def test_banker_rounding_no_false_positive(self, conn):
        """LCARS 17.0 / 2 = 8.5 → banker's rounds to 8.
        MAL stores 8 → consistent, no false positive."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=17.0)
        conn.commit()

        # round(17.0 / 2) = round(8.5) = 8 (banker's rounds to even)
        with _fake_mal_list([_mal(200, 8)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}

    def test_unscored_externally_skipped(self, conn):
        """MAL score = 0 (unscored by convention) → skip, no flag."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        with _fake_mal_list([_mal(200, 0)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 0, "flagged": 0}

    def test_not_on_mal_list_skipped(self, conn):
        """Season has mal_id but isn't in the fetched list → skip."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        with _fake_mal_list([]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 0, "flagged": 0}


# ---------------------------------------------------------------------------
# Drift detected (MAL)
# ---------------------------------------------------------------------------


class TestMalDrift:
    def setup_method(self, _):
        config.get_current().mal_access_token = "mal-token"

    def test_drift_opens_pending_review(self, conn):
        """MAL score changed externally → opens pending_review.
        proposed_value_chain entry = str(mal_score * 2)."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        # LCARS would push round(16/2)=8; MAL now shows 9 (user changed it)
        with _fake_mal_list([_mal(200, 9)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        pr = conn.execute(
            "SELECT field, source, previous_value, proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()
        assert pr is not None
        assert pr["field"] == "score"
        assert pr["source"] == "mal_score_drift"
        assert pr["previous_value"] == "16.0"  # was the LCARS season score
        chain = json.loads(pr["proposed_value_chain"])
        assert chain == ["18.0"]  # 9 * 2 = 18.0

    def test_lcars_no_score_mal_has_score(self, conn):
        """LCARS has no score, MAL does → flag as inbound score."""
        _show(conn, "s-aaaaaa", score=None)
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=None)
        conn.commit()

        with _fake_mal_list([_mal(200, 7)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        pr = conn.execute(
            "SELECT previous_value, proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()
        assert pr is not None
        assert pr["previous_value"] is None
        chain = json.loads(pr["proposed_value_chain"])
        assert chain == ["14.0"]  # 7 * 2 = 14.0


# ---------------------------------------------------------------------------
# Idempotency (MAL)
# ---------------------------------------------------------------------------


class TestMalIdempotency:
    def setup_method(self, _):
        config.get_current().mal_access_token = "mal-token"

    def test_second_run_extends_not_duplicates(self, conn):
        """Same MAL drift on back-to-back calls → chain extended, no duplicate row."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        with _fake_mal_list([_mal(200, 9)]):
            score_sync.check_mal_score_drift(conn)
            result = score_sync.check_mal_score_drift(conn)

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
        """Human resolved the exact proposed MAL value → no re-open on next tick."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        with _fake_mal_list([_mal(200, 9)]):
            score_sync.check_mal_score_drift(conn)

        conn.execute(
            "UPDATE pending_review SET resolved_at = 'now'"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        )
        conn.commit()

        with _fake_mal_list([_mal(200, 9)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 1, "flagged": 0}
        open_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()[0]
        assert open_count == 0

    def test_new_value_after_resolve_reopens(self, conn):
        """A genuinely different MAL score after resolving reopens the review."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        conn.commit()

        with _fake_mal_list([_mal(200, 9)]):
            score_sync.check_mal_score_drift(conn)
        conn.execute(
            "UPDATE pending_review SET resolved_at = 'now'"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
        )
        conn.commit()

        with _fake_mal_list([_mal(200, 10)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 1, "flagged": 1}
        open_count = conn.execute(
            "SELECT COUNT(*) FROM pending_review"
            " WHERE entity_type = 'season' AND entity_id = 'z-aaaaaa'"
            "   AND resolved_at IS NULL"
        ).fetchone()[0]
        assert open_count == 1


# ---------------------------------------------------------------------------
# Multiple seasons (MAL)
# ---------------------------------------------------------------------------


class TestMalMultiple:
    def setup_method(self, _):
        config.get_current().mal_access_token = "mal-token"

    def test_mixed_clean_and_drift(self, conn):
        """Two MAL-linked seasons: one clean, one drifted → only the drifted one flagged."""
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=16.0)
        _season_with_mal(conn, "z-bbbbbb", "s-aaaaaa", 2, 201, score=14.0)
        conn.commit()

        # z-aaaaaa: push=round(16/2)=8, MAL=8 → clean
        # z-bbbbbb: push=round(14/2)=7, MAL=9 → drift
        with _fake_mal_list([_mal(200, 8), _mal(201, 9)]):
            result = score_sync.check_mal_score_drift(conn)

        assert result == {"checked": 2, "flagged": 1}
        flagged_id = conn.execute(
            "SELECT entity_id FROM pending_review WHERE resolved_at IS NULL"
        ).fetchone()
        assert flagged_id["entity_id"] == "z-bbbbbb"


class TestMalHalfStep:
    """2026-09-23 — an LCARS score that lands exactly between two MAL
    integers (13.0 -> 6.5) is consistent with either neighbour; only a
    real disagreement (more than one conversion step) is drift."""

    def setup_method(self, _):
        config.get_current().mal_access_token = "mal-token"

    def test_half_step_either_neighbour_is_consistent(self, conn):
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=13.0)
        _season_with_mal(conn, "z-bbbbbb", "s-aaaaaa", 2, 201, score=13.0)
        conn.commit()
        with _fake_mal_list([_mal(200, 6), _mal(201, 7)]):
            result = score_sync.check_mal_score_drift(conn)
        assert result == {"checked": 2, "flagged": 0}

    def test_more_than_one_step_is_still_drift(self, conn):
        _show(conn, "s-aaaaaa")
        _season_with_mal(conn, "z-aaaaaa", "s-aaaaaa", 1, 200, score=14.0)
        conn.commit()
        with _fake_mal_list([_mal(200, 6)]):
            result = score_sync.check_mal_score_drift(conn)
        assert result == {"checked": 1, "flagged": 1}
