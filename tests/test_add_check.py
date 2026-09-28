"""The one add check (PLAN-CODE phase 5.1) — RULEBOOK R3.1–R3.7a, R4.7."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import add_check
from lcars.add_check import LIST, RELATION, SONARR, USER, Candidate

NOW = "2026-01-01T00:00:00Z"
TVDB = 371310

# Fribb rows (anime-lists): Mushoku Tensei's shape.
DATASET = [
    {"anilist_id": 108465, "mal_id": 39535, "tvdb_id": TVDB, "type": "TV", "season": {"tvdb": 1}},
    {"anilist_id": 127720, "mal_id": 45576, "tvdb_id": TVDB, "type": "TV", "season": {"tvdb": 1},
     "episode_offset": {"tvdb": 11}},
    {"anilist_id": 146065, "mal_id": 51179, "tvdb_id": TVDB, "type": "TV", "season": {"tvdb": 2}},
    {"anilist_id": 178789, "mal_id": 59193, "tvdb_id": TVDB, "type": "TV", "season": {"tvdb": 3}},
    {"anilist_id": 150000, "mal_id": 60000, "tvdb_id": TVDB, "type": "OVA", "season": {"tvdb": 0}},
    {"anilist_id": 500, "mal_id": 501, "tvdb_id": 999, "type": "TV", "season": {"tvdb": 1}},
]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "add.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, title_english,"
        " primary_title, status, created_at, updated_at) VALUES ('s-mush01', 'episodic',"
        " 'anime', 'Mushoku Tensei: Isekai Ittara Honki Dasu', 'Mushoku Tensei: Jobless"
        " Reincarnation', 'romaji', 'watching', ?, ?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-mush01', 'tvdb', ?, '', ?)",
        (str(TVDB), NOW),
    )
    seasons = (("z-mush01", 1, 108465), ("z-mush02", 2, None), ("z-mush03", 3, 178789))
    for zid, n, anilist in seasons:
        c.execute(
            "INSERT INTO season (id, show_id, season_number, anilist_id, source, status,"
            " created_at, updated_at) VALUES (?, 's-mush01', ?, ?, 'fribb', 'watching', ?, ?)",
            (zid, n, anilist, NOW, NOW),
        )
    yield c
    c.close()


def _c(origin, **kw):
    kw.setdefault("titles", ["Mushoku Tensei"])
    return Candidate(origin, **kw)


def test_a_season_already_linked_is_already_tracked(conn):
    d = add_check.classify(conn, _c(LIST, anilist_id=108465), DATASET)
    assert (d.kind, d.season_id) == ("already_tracked", "z-mush01")


def test_second_cour_of_a_tracked_tvdb_season_is_a_part(conn):
    d = add_check.classify(conn, _c(LIST, anilist_id=127720), DATASET)
    assert (d.kind, d.season_id, d.season_number) == ("part", "z-mush01", 1)


def test_tvdb_season_without_list_ids_gets_this_one(conn):
    d = add_check.classify(conn, _c(RELATION, anilist_id=146065), DATASET)
    assert (d.kind, d.season_id) == ("link_season", "z-mush02")


def test_season_0_piece_is_a_special_level(conn):
    d = add_check.classify(conn, _c(RELATION, anilist_id=150000, media_type="OVA"), DATASET)
    assert d.kind == "special"


def test_related_entry_of_an_untracked_tvdb_show_is_not_added(conn):
    d = add_check.classify(conn, _c(RELATION, anilist_id=500, titles=["Other"]), DATASET)
    assert d.kind == "not_added"  # R3.5


def test_your_add_of_an_untracked_tvdb_show_is_a_new_show(conn):
    d = add_check.classify(conn, _c(USER, anilist_id=500, titles=["Other"]), DATASET)
    assert (d.kind, d.tvdb_id) == ("new_show", 999)


def test_tvdb_not_listing_it_yet_goes_to_you_with_the_prequels_show(conn):
    # AniList 217434 (Mushoku Tensei III part 2): not in Fribb yet, sequel of 178789.
    c = _c(LIST, anilist_id=217434, prequel_anilist_ids=[178789])
    d = add_check.classify(conn, c, DATASET)
    assert (d.kind, d.show_id) == ("needs_user", "s-mush01")  # R3.7a
    assert "after AniList 178789" in d.proposal


def test_no_tvdb_id_at_all(conn):
    assert add_check.classify(conn, _c(LIST, anilist_id=42), DATASET).kind == "individual_season"
    assert add_check.classify(conn, _c(RELATION, anilist_id=42), DATASET).kind == "not_added"


def test_sonarr_tvdb_id_disagreeing_with_fribb_goes_to_you(conn):
    d = add_check.classify(conn, _c(SONARR, anilist_id=500, tvdb_id=TVDB), DATASET)
    assert d.kind == "needs_user"


def test_title_search_alone_is_never_accepted(conn):
    # A TVDB id with no independent source (not Sonarr, not you, not Fribb).
    d = add_check.classify(conn, _c(LIST, anilist_id=42, tvdb_id=12345), DATASET)
    assert d.kind == "needs_user"  # R3.7


def test_sonarr_series_with_a_different_name_goes_to_you(conn):
    c = _c(SONARR, tvdb_id=777, tvdb_name="Completely Different", titles=["Some Anime"])
    assert add_check.classify(conn, c, DATASET).kind == "needs_user"
    c = _c(SONARR, tvdb_id=777, tvdb_name="Some Anime", titles=["Some Anime"])
    assert add_check.classify(conn, c, DATASET).kind == "new_show"


def test_sonarr_add_latest_season_planned_earlier_skipped(conn):
    # R5.3: none tracked yet → latest planned, every earlier one skipped.
    add_check.apply_sonarr_initial_statuses(conn, "s-mush01")
    statuses = [r[0] for r in conn.execute(
        "SELECT status FROM season WHERE show_id = 's-mush01' ORDER BY season_number")]
    assert statuses == ["skipped", "skipped", "planned"]
    show = conn.execute("SELECT status FROM show WHERE id = 's-mush01'").fetchone()[0]
    assert show == "planned"


def test_sonarr_series_of_a_tracked_show_is_left_alone(conn):
    assert add_check.add_sonarr_series(conn, TVDB, "Mushoku Tensei", "anime", "webhook") == (
        "already_tracked", "s-mush01"
    )
