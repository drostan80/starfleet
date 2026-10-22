"""Episode.isSeasonFinale — the calendar's Month view outlines the last episode of a season."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import db, resolvers

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _reset_db():
    db.close()
    yield
    db.close()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "finale.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    c = db.connect(path)
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, created_at, updated_at)"
        " VALUES ('s-fin001', 'episodic', 'anime', 'Show', 'english', 'watching', ?, ?)",
        (NOW, NOW),
    )
    # S1: AniList's total (3) equals the episodes held; S2: an AniList entry that gave no total yet
    for zid, number, total, anilist in (("z-fin001", 1, 3, 111), ("z-fin002", 2, None, 222)):
        c.execute(
            "INSERT INTO season (id, show_id, season_number, part_number, source, kind, status,"
            " anilist_id, episode_total, created_at, updated_at)"
            " VALUES (?, 's-fin001', ?, 1, 'manual', 'tvdb_season', 'watching', ?, ?, ?, ?)",
            (zid, number, anilist, total, NOW, NOW),
        )
    n = 0
    for season, count, kind in ((0, 2, "special"), (1, 3, "regular"), (2, 2, "regular")):
        for e in range(1, count + 1):
            n += 1
            c.execute(
                "INSERT INTO episode (id, show_id, season, episode, kind, created_at, updated_at)"
                " VALUES (?, 's-fin001', ?, ?, ?, ?, ?)",
                (f"e-fin{n:03d}", season, e, kind, NOW, NOW),
            )
    c.commit()
    return c


def _finale(season, episode, kind="regular"):
    return resolvers.resolve_episode_is_season_finale(
        {"show_id": "s-fin001", "season": season, "episode": episode, "kind": kind}, None)


def test_the_last_episode_of_a_season_with_a_confirmed_length_is_the_finale(conn):
    assert [_finale(1, e) for e in (1, 2, 3)] == [False, False, True]


def test_a_season_whose_length_is_not_known_yet_has_no_finale(conn):
    # still airing: the episodes held are only the ones so far (R2.15a)
    assert [_finale(2, e) for e in (1, 2)] == [False, False]


def test_a_special_is_never_a_finale(conn):
    assert _finale(0, 2, "special") is False
    assert _finale(1, 3, "special") is False
