"""_propose_sequel_seasons should still propose a sequel-season review
when the sequel's AniList ID only appears on the orphan stub itself,
not when it's properly mapped on an unrelated show.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import metadata


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "sequel_test.db"
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


def _show(conn, show_id, title, tracked=1):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'romaji', 'watching', ?, 'x', 'x')",
        (show_id, title, tracked),
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


def _anilist_media(sequel_id, sequel_title="Sequel", sequel_format="TV"):
    return {
        "relations": {
            "edges": [
                {
                    "relationType": "SEQUEL",
                    "node": {
                        "id": sequel_id,
                        "format": sequel_format,
                        "title": {"romaji": sequel_title},
                        "idMal": None,
                    },
                }
            ]
        }
    }


class TestProposeSequelSeasons:
    def test_proposes_when_sequel_id_only_on_stub(self, conn):
        """The orphan-stub scenario: sequel AniList ID exists as S1 on the
        stub show, but not on any other show. Should still open a review."""
        _show(conn, "s-par001", "Frieren", tracked=1)
        _external_id(conn, "s-par001", "anilist", "154587")
        _external_id(conn, "s-par001", "sonarr", "frieren")
        _season(conn, "z-aaa001", "s-par001", 1, anilist_id=154587)

        _show(conn, "s-stb001", "Frieren Part 2", tracked=1)
        _external_id(conn, "s-stb001", "anilist", "189513")
        _external_id(conn, "s-stb001", "sonarr", "frieren")
        _season(conn, "z-aaa002", "s-stb001", 1, anilist_id=189513)

        conn.commit()

        media = _anilist_media(189513, "Frieren Part 2")
        show = {"id": "s-par001", "tracked": True}
        metadata._propose_sequel_seasons(conn, show, media)
        conn.commit()

        row = conn.execute(
            "SELECT 1 FROM pending_review"
            " WHERE entity_id = 's-par001' AND field LIKE 'sequel_season:%'"
            " AND resolved_at IS NULL"
        ).fetchone()
        assert row is not None, "Should have opened a sequel-season review"

    def test_skips_when_sequel_id_mapped_on_different_show(self, conn):
        """The sequel AniList ID is mapped as a season on a different,
        legitimate show — not a stub. Should NOT propose."""
        _show(conn, "s-par002", "Show A", tracked=1)
        _external_id(conn, "s-par002", "anilist", "100")
        _season(conn, "z-bbb001", "s-par002", 1, anilist_id=100)

        _show(conn, "s-oth001", "Show B", tracked=1)
        _external_id(conn, "s-oth001", "anilist", "200")
        _season(conn, "z-bbb002", "s-oth001", 1, anilist_id=200)

        conn.commit()

        media = _anilist_media(200, "Show B")
        show = {"id": "s-par002", "tracked": True}
        metadata._propose_sequel_seasons(conn, show, media)
        conn.commit()

        row = conn.execute(
            "SELECT 1 FROM pending_review"
            " WHERE entity_id = 's-par002' AND field LIKE 'sequel_season:%'"
            " AND resolved_at IS NULL"
        ).fetchone()
        assert row is None, "Should NOT propose — already mapped on another show"

    def test_proposes_when_sequel_id_unmapped_anywhere(self, conn):
        """Sequel AniList ID doesn't exist in any season row. Should propose."""
        _show(conn, "s-par003", "Show A", tracked=1)
        _external_id(conn, "s-par003", "anilist", "100")
        _season(conn, "z-ccc001", "s-par003", 1, anilist_id=100)
        conn.commit()

        media = _anilist_media(999, "New Sequel")
        show = {"id": "s-par003", "tracked": True}
        metadata._propose_sequel_seasons(conn, show, media)
        conn.commit()

        row = conn.execute(
            "SELECT 1 FROM pending_review"
            " WHERE entity_id = 's-par003' AND field LIKE 'sequel_season:%'"
            " AND resolved_at IS NULL"
        ).fetchone()
        assert row is not None, "Should propose for unmapped sequel"
