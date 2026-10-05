"""scripts/cleanup_duplicate_special_levels_20261005.py — keeps the oldest copy of each special
level, deletes only plain auto-created duplicates, dry run by default."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "cleanup_dups", ROOT / "scripts" / "cleanup_duplicate_special_levels_20261005.py"
)
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)

T = "2026-10-01T00:00:00Z"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "dups.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(path)
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " created_at, updated_at) VALUES ('s-dup001', 'episodic', 'tv', 'Dup', 'romaji',"
        " 'watching', ?, ?)", (T, T))
    c.execute(
        "INSERT INTO season (id, show_id, season_number, source, kind, created_at, updated_at)"
        " VALUES ('z-dup000', 's-dup001', 1, 'manual', 'tvdb_season', ?, ?)", (T, T))
    c.commit()
    c.close()
    return path


def _level(c, sid, label, created, parent="z-dup000", **cols):
    cols = {"anilist_id": None, "score": None, **cols}
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, parent_id, label, source,"
        " status, list_sync, anilist_id, score, created_at, updated_at)"
        " VALUES (?, 's-dup001', NULL, 'special', ?, ?, 'auto', 'planned', 0, ?, ?, ?, ?)",
        (sid, parent, label, cols["anilist_id"], cols["score"], created, created))
    c.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, 3, 3)", (sid,))


def _ids(c):
    return {r[0] for r in c.execute("SELECT id FROM season WHERE kind = 'special'")}


def test_dry_run_changes_nothing_and_apply_keeps_the_oldest_of_each_group(db_path):
    c = sqlite3.connect(db_path)
    for i, created in enumerate(["2026-09-30T06:00:00Z", "2026-10-01T06:00:00Z",
                                 "2026-10-02T06:00:00Z"]):
        _level(c, f"z-pa{i:04d}", "S00E03 Podcast", created)
    _level(c, "z-pb0000", "S00E04 Other", "2026-09-30T06:00:00Z")  # not duplicated
    c.commit()

    dry = cleanup.run(c, apply=False)
    assert dry["extras"] == 2 and dry["deletable"] == 2 and dry["applied"] is False
    assert _ids(c) == {"z-pa0000", "z-pa0001", "z-pa0002", "z-pb0000"}

    done = cleanup.run(c, apply=True)
    assert done["applied"] is True and done["seasons_after"] == done["seasons_before"] - 2
    assert _ids(c) == {"z-pa0000", "z-pb0000"}  # the oldest of the group, and the unique one
    assert c.execute("SELECT COUNT(*) FROM season_span WHERE season_id IN ('z-pa0001','z-pa0002')"
                     ).fetchone()[0] == 0  # spans went with them
    again = cleanup.run(c, apply=True)  # idempotent
    assert again["extras"] == 0


def test_a_copy_that_is_more_than_a_plain_duplicate_is_left_alone(db_path):
    c = sqlite3.connect(db_path)
    _level(c, "z-qa0000", "S00E05 X", "2026-09-30T06:00:00Z")
    _level(c, "z-qa0001", "S00E05 X", "2026-10-01T06:00:00Z", score=8.0)  # has a score
    _level(c, "z-qa0002", "S00E05 X", "2026-10-01T07:00:00Z", anilist_id=999)  # has a list id
    _level(c, "z-qa0003", "S00E05 X", "2026-10-01T08:00:00Z")  # plain, but has a child level
    _level(c, "z-qa0004", "S00E05 X", "2026-10-01T09:00:00Z", parent="z-qa0003")
    _level(c, "z-qa0005", "S00E05 X", "2026-10-01T10:00:00Z")  # plain copy
    c.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, season_id, created_at,"
        " updated_at) VALUES ('e-qa0001', 's-dup001', 0, 1, 'special', 'z-qa0005', ?, ?)", (T, T))
    c.commit()

    result = cleanup.run(c, apply=True)


    assert _ids(c) == {"z-qa0000", "z-qa0001", "z-qa0002", "z-qa0003", "z-qa0004", "z-qa0005"}
    assert result["deletable"] == 0 and result["left_alone"] == 3


def test_an_id_less_twin_beside_a_level_that_holds_an_id_is_not_a_duplicate_to_remove(db_path):
    # Made once when AniDB data for the piece wasn't there yet; recreated by the next pass if
    # deleted, and not growing — so the cleanup leaves it.
    c = sqlite3.connect(db_path)
    _level(c, "z-ra0000", "Side piece after season 2", "2026-09-30T06:37:00Z", anilist_id=204431)
    _level(c, "z-ra0001", "Side piece after season 2", "2026-09-30T08:28:00Z")
    c.commit()
    result = cleanup.run(c, apply=True)
    assert result["extras"] == 0
    assert _ids(c) == {"z-ra0000", "z-ra0001"}
