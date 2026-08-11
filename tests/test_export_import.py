"""JSON export/import — SCOPE.md §6.12, BUILD_PLAN.md A.15.

Exercised against two real, separately-migrated SQLite databases (the
actual restore scenario: rebuilding a fresh LCARS instance) — a
hand-rolled mini-schema would drift from the real one (generated
columns, real FK constraints) and miss exactly the bugs this needs to
catch.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import export_import


def _migrated_db(path: Path) -> sqlite3.Connection:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture
def source_db(tmp_path) -> sqlite3.Connection:
    return _migrated_db(tmp_path / "source.db")


@pytest.fixture
def target_db(tmp_path) -> sqlite3.Connection:
    return _migrated_db(tmp_path / "target.db")


def _seed(conn: sqlite3.Connection) -> None:
    """A small but genuinely interconnected dataset — one show, one
    episode (with its generated available_locally column populated via
    available_via_sonarr), one watch_event, one tag linked to the show,
    one franchise with the show as a member — enough to exercise
    dependency ordering across several of EXPORT_IMPORT_TABLES' tiers,
    not just a single flat table."""
    conn.execute(
        "INSERT INTO show"
        " (id, media_shape, tracking_space, title_romaji, primary_title, status, tracked,"
        "  created_at, updated_at)"
        " VALUES ('s-exp001', 'episodic', 'anime', 'Export Show', 'romaji', 'watching', 1,"
        "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO episode"
        " (id, show_id, season, episode, kind, state, available_via_sonarr,"
        "  created_at, updated_at)"
        " VALUES ('e-exp001', 's-exp001', 1, 1, 'regular', 'watched', 'available',"
        "  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
        " VALUES ('w-exp001', 's-exp001', 1, 1, '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO tag (id, name, created_at)"
        " VALUES ('t-exp001', 'favorite', '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO show_tag (show_id, tag_id) VALUES ('s-exp001', 't-exp001')")
    conn.execute("INSERT INTO franchise (id, name) VALUES ('f-exp001', 'Export Franchise')")
    conn.execute(
        "INSERT INTO franchise_member (franchise_id, show_id, sort_order)"
        " VALUES ('f-exp001', 's-exp001', 1)"
    )
    conn.commit()


def test_export_includes_every_table(source_db):
    blob = export_import.export_data(source_db)
    data = json.loads(blob)
    assert data["schema_version"] == export_import.SCHEMA_VERSION
    assert set(data["tables"]) == set(export_import.EXPORT_IMPORT_TABLES)
    assert len(data["tables"]) == 26  # 25 through B.3 + show_merge (B.14)


def test_export_import_round_trip_restores_everything(source_db, target_db):
    _seed(source_db)
    blob = export_import.export_data(source_db)

    counts = export_import.import_data(target_db, blob)
    assert counts["show"] == 1
    assert counts["episode"] == 1
    assert counts["watch_event"] == 1
    assert counts["tag"] == 1
    assert counts["show_tag"] == 1
    assert counts["franchise"] == 1
    assert counts["franchise_member"] == 1

    show = dict(target_db.execute("SELECT * FROM show WHERE id = 's-exp001'").fetchone())
    assert show["title_romaji"] == "Export Show"

    episode = dict(target_db.execute("SELECT * FROM episode WHERE id = 'e-exp001'").fetchone())
    # the generated column: excluded from the INSERT, recomputed fresh by SQLite
    # itself on the target db — not carried over as a literal exported value
    assert episode["available_locally"] == 1

    watch_event = target_db.execute("SELECT 1 FROM watch_event WHERE id = 'w-exp001'").fetchone()
    assert watch_event is not None

    show_tag = target_db.execute(
        "SELECT 1 FROM show_tag WHERE show_id = 's-exp001' AND tag_id = 't-exp001'"
    ).fetchone()
    assert show_tag is not None

    franchise_member = target_db.execute(
        "SELECT sort_order FROM franchise_member WHERE franchise_id = 'f-exp001'"
    ).fetchone()
    assert franchise_member["sort_order"] == 1


def test_import_rejects_schema_version_mismatch(target_db):
    blob = json.dumps({"schema_version": 999, "tables": {}})
    with pytest.raises(ValueError, match="schema_version mismatch"):
        export_import.import_data(target_db, blob)
    # nothing should have been touched
    assert target_db.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0


def test_import_rejects_malformed_json(target_db):
    with pytest.raises(ValueError, match="not valid JSON"):
        export_import.import_data(target_db, "{not json")


def test_import_colliding_id_raises_plain_integrity_error(source_db, target_db):
    """§6.12: "colliding ids surface as an ordinary IntegrityError, not
    something this handles specially" — confirmed by *not* catching it
    into some friendlier shape."""
    _seed(source_db)
    blob = export_import.export_data(source_db)
    export_import.import_data(target_db, blob)  # first import succeeds
    with pytest.raises(sqlite3.IntegrityError):
        export_import.import_data(target_db, blob)  # re-importing the same data collides


def test_import_rolls_back_a_collision_that_happens_partway_through(source_db, target_db):
    """The whole import is one transaction — a collision on a *later*
    table (tag is ordered well after show in EXPORT_IMPORT_TABLES)
    shouldn't leave the *earlier* successfully-inserted show row
    behind either."""
    _seed(source_db)
    blob = export_import.export_data(source_db)

    # pre-seed target with a colliding tag id — show itself has no
    # collision, so its INSERT succeeds before the tag one fails
    target_db.execute(
        "INSERT INTO tag (id, name, created_at)"
        " VALUES ('t-exp001', 'already here', '2020-01-01T00:00:00Z')"
    )
    target_db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        export_import.import_data(target_db, blob)

    # the show row that succeeded earlier in the same call is gone too
    assert target_db.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0
