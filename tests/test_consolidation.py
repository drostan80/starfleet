"""Same-TVDB consolidation plan (PLAN-CODE phase 5) — RULEBOOK R1.14. Read-only."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import consolidation

NOW = "2026-01-01T00:00:00Z"
DATASET = [
    {"anilist_id": 1, "tvdb_id": 500, "type": "TV", "season": {"tvdb": 1}},
    {"anilist_id": 2, "tvdb_id": 500, "type": "TV", "season": {"tvdb": 1},
     "episode_offset": {"tvdb": 12}},
    {"anilist_id": 3, "tvdb_id": 500, "type": "TV", "season": {"tvdb": 2}},
    {"anilist_id": 4, "tvdb_id": 500, "type": "OVA", "season": {"tvdb": 0}},
    {"anilist_id": 9, "tvdb_id": 500, "type": "MOVIE", "season": {"tvdb": 0}},
]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "c.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(path)
    for sid, shape, anilist, created in (
        ("s-main01", "episodic", 1, "2026-01-01"), ("s-cour02", "episodic", 2, "2026-01-02"),
        ("s-seas02", "episodic", 3, "2026-01-03"), ("s-ova001", "episodic", 4, "2026-01-04"),
        ("s-film01", "movie", 9, "2026-01-05"),
    ):
        c.execute(
            "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
            " status, created_at, updated_at) VALUES (?, ?, 'anime', ?, 'romaji', 'planned', ?, ?)",
            (sid, shape, sid, created, created),
        )
        c.execute(
            "INSERT INTO season (id, show_id, season_number, anilist_id, source, status,"
            " created_at, updated_at) VALUES (?, ?, 1, ?, 'manual', 'planned', ?, ?)",
            (f"z-{sid[2:]}", sid, anilist, NOW, NOW),
        )
        if shape == "episodic":
            c.execute(
                "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                " VALUES (?, 'tvdb', '500', '', ?)", (sid, NOW),
            )
    c.commit()
    yield c
    c.close()


def test_plan_lists_each_show_as_the_level_it_becomes(conn):
    p = consolidation.plan(conn, DATASET, {})
    (group,) = p["groups"]
    assert group["winner"] == "s-main01"  # holds TVDB season 1
    becomes = {s["show_id"]: s["becomes"] for s in group["shows"]}
    assert becomes["s-cour02"].startswith("part of TVDB season 1 (cour from episode 13)")
    assert becomes["s-seas02"] == "new TVDB season 2"
    assert becomes["s-ova001"].startswith("season-0 piece")
    assert p["films"] == [
        {"show_id": "s-film01", "title": "s-film01", "into": "s-main01", "tvdb_id": 500}
    ]
    # Read-only: nothing changed.
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 5
