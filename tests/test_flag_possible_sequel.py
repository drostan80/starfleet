"""shows.flag_possible_sequel — closes the gap where an auto-created
show (Sonarr/Radarr SeriesAdd/MovieAdded webhook, reconcileArrState's
untracked-show discovery) is a sequel of an already-tracked show, but
create_show's own pre-insert find_sequel_parent call had no anilist_id
to work with yet at insert time. By the time create_show returns, its
inline metadata.fetch_and_populate has already resolved the anilist_id
and written show_relation rows — flag_possible_sequel re-checks then.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import shows


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "flag_sequel_test.db"
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


def _show(conn, show_id, title, tracked=1, tracking_space="anime"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, ?, 'romaji', 'watching', ?, 'x', 'x')",
        (show_id, tracking_space, title, tracked),
    )


def _external_id(conn, show_id, service, external_id):
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, 'https://x', 'x')",
        (show_id, service, external_id),
    )


def _season(conn, season_id, show_id, season_number, anilist_id=None):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id,"
        " source, matched, manual_override, created_at, updated_at)"
        " VALUES (?, ?, ?, 'watching', ?, 'fribb', 0, 0, 'x', 'x')",
        (season_id, show_id, season_number, anilist_id),
    )


def _relation(conn, show_id, related_show_id, relation_type):
    conn.execute(
        "INSERT INTO show_relation (show_id, related_show_id, relation_type, created_at)"
        " VALUES (?, ?, ?, 'x')",
        (show_id, related_show_id, relation_type),
    )


class TestFlagPossibleSequel:
    def test_flags_review_when_new_show_lists_tracked_parent_as_prequel(self, conn):
        """The exact scenario a Sonarr SeriesAdd/reconcile auto-create hits:
        the new show's own AniList relations (fetched moments after it was
        created, inline in fetch_and_populate) list an already-tracked show
        as its PREQUEL — the id-blind pre-insert check couldn't have caught
        this, so this post-insert re-check should."""
        _show(conn, "s-par001", "Frieren", tracked=1)
        _external_id(conn, "s-par001", "anilist", "154587")
        _season(conn, "z-aaa001", "s-par001", 1, anilist_id=154587)

        _show(conn, "s-new001", "Frieren Part 2", tracked=1)
        _external_id(conn, "s-new001", "anilist", "189513")
        _relation(conn, "s-new001", "s-par001", "PREQUEL")
        conn.commit()

        shows.flag_possible_sequel(conn, "s-new001")

        row = conn.execute(
            "SELECT proposed_value_chain FROM pending_review"
            " WHERE entity_type = 'show' AND entity_id = 's-new001'"
            "   AND field = 'possible_sequel_of:s-par001' AND resolved_at IS NULL"
        ).fetchone()
        assert row is not None
        assert "s-par001" in row["proposed_value_chain"]
        assert "Frieren" in row["proposed_value_chain"]

    def test_noop_when_no_anilist_id(self, conn):
        """Non-anime auto-creates (Radarr movies, plain TV via Sonarr) never
        get an anilist_id resolved at all — nothing to check."""
        _show(conn, "s-tv0001", "Some TV Show", tracked=1, tracking_space="tv")
        conn.commit()

        shows.flag_possible_sequel(conn, "s-tv0001")

        row = conn.execute(
            "SELECT 1 FROM pending_review WHERE entity_id = 's-tv0001'"
        ).fetchone()
        assert row is None

    def test_noop_when_no_sequel_relationship_found(self, conn):
        _show(conn, "s-new002", "Standalone Show", tracked=1)
        _external_id(conn, "s-new002", "anilist", "999999")
        conn.commit()

        shows.flag_possible_sequel(conn, "s-new002")

        row = conn.execute(
            "SELECT 1 FROM pending_review WHERE entity_id = 's-new002'"
        ).fetchone()
        assert row is None

    def test_does_not_duplicate_on_repeated_calls(self, conn):
        _show(conn, "s-par003", "Frieren", tracked=1)
        _external_id(conn, "s-par003", "anilist", "154587")
        _season(conn, "z-ccc001", "s-par003", 1, anilist_id=154587)

        _show(conn, "s-new003", "Frieren Part 2", tracked=1)
        _external_id(conn, "s-new003", "anilist", "189513")
        _relation(conn, "s-new003", "s-par003", "PREQUEL")
        conn.commit()

        shows.flag_possible_sequel(conn, "s-new003")
        shows.flag_possible_sequel(conn, "s-new003")

        rows = conn.execute(
            "SELECT id FROM pending_review"
            " WHERE entity_id = 's-new003' AND resolved_at IS NULL"
        ).fetchall()
        assert len(rows) == 1

    def test_does_not_reopen_after_resolved_with_same_value(self, conn):
        """Once a human resolves this exact review, a later refresh cycle
        re-running the same check must not reopen it."""
        _show(conn, "s-par004", "Frieren", tracked=1)
        _external_id(conn, "s-par004", "anilist", "154587")
        _season(conn, "z-ddd001", "s-par004", 1, anilist_id=154587)

        _show(conn, "s-new004", "Frieren Part 2", tracked=1)
        _external_id(conn, "s-new004", "anilist", "189513")
        _relation(conn, "s-new004", "s-par004", "PREQUEL")
        conn.commit()

        shows.flag_possible_sequel(conn, "s-new004")
        conn.execute(
            "UPDATE pending_review SET resolved_at = 'x'"
            " WHERE entity_id = 's-new004' AND field = 'possible_sequel_of:s-par004'"
        )
        conn.commit()

        shows.flag_possible_sequel(conn, "s-new004")

        rows = conn.execute(
            "SELECT id FROM pending_review"
            " WHERE entity_id = 's-new004' AND resolved_at IS NULL"
        ).fetchall()
        assert len(rows) == 0
