"""scripts/make_parts_20261005.py — a TVDB season holding two AniList cours becomes part levels."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("make_parts", ROOT / "scripts"
                                               / "make_parts_20261005.py")
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)
T = "2026-10-05T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "parts.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " tracked, created_at, updated_at)"
        " VALUES ('s-mkp001', 'episodic', 'anime', 'Show', 'romaji', 'watching', 1, ?, ?)",
        (T, T))
    # TVDB S3: 24 episodes, abs 49-72, with a gap at 59.5 (a mini of its own level)
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, mal_id, episode_total,"
        " status, source, created_at, updated_at) VALUES ('z-mkp000', 's-mkp001', 3,"
        " 'tvdb_season', 195516, 61987, 12, 'watching', 'fribb', ?, ?)", (T, T))
    for lo, hi in ((49, 59), (60, 72)):
        c.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES ('z-mkp000', ?, ?)",
                  (lo, hi))
    c.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
              " VALUES ('z-mkp000', 'anilist', 195516, ?)", (T,))
    c.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
              " VALUES ('z-mkp000', 'mal', 61987, ?)", (T,))
    # the leftover "S4" row holding the second cour
    c.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, mal_id, status, source,"
        " created_at, updated_at) VALUES ('z-mkp004', 's-mkp001', 4, 'tvdb_season', 200927,"
        " 62841, 'watching', 'fribb', ?, ?)", (T, T))
    for n in range(1, 25):
        c.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number, state,"
            " season_id, created_at, updated_at)"
            " VALUES (?, 's-mkp001', 3, ?, 'regular', ?, ?, 'z-mkp000', ?, ?)",
            (f"e-mkp{n:03d}", n, 48 + n, "watched" if n == 1 else "unwatched", T, T))
    c.execute(
        "INSERT INTO pending_review (id, entity_type, entity_id, field, proposed_value_chain,"
        " source, created_at) VALUES ('r-mkp001', 'season', 'z-mkp000', 'anilist_id',"
        " '[\"season 3 has 24 episode(s)\"]', 'anilist', ?)", (T,))
    c.commit()
    return c


def test_a_season_with_two_cours_becomes_a_parent_with_two_parts(conn):
    p = mp.plan(conn, "s-mkp001", 3, [195516, 200927])
    assert [s["episodes"] for s in p["sources"]] == [12, 12]
    assert p["sources"][0]["spans"] == [(49.0, 59.0), (60.0, 60.0)]  # follows the season's gap
    assert p["sources"][1]["spans"] == [(61.0, 72.0)]
    mp.apply(conn, p)

    parent = conn.execute("SELECT * FROM season WHERE id = 'z-mkp000'").fetchone()
    assert (parent["kind"], parent["anilist_id"], parent["mal_id"]) == ("tvdb_season", None, None)
    parts = conn.execute("SELECT * FROM season WHERE parent_id = 'z-mkp000' ORDER BY part_number"
                         ).fetchall()
    assert [(r["kind"], r["season_number"], r["part_number"], r["anilist_id"], r["mal_id"])
            for r in parts] == [("part", 3, 1, 195516, 61987), ("part", 3, 2, 200927, 62841)]
    assert parts[1]["id"] == "z-mkp004"  # the leftover row itself became part 2
    assert [(r["abs_start"], r["abs_end"], r["status"]) for r in parts] == [
        (49, 60, "watching"), (61, 72, "watching")]
    # the list-id rows moved to the new part 1 with the ids
    assert {(r[0], r[1]) for r in conn.execute(
        "SELECT service, season_id FROM season_external_id")} == {
        ("anilist", parts[0]["id"]), ("mal", parts[0]["id"])}
    assert conn.execute("SELECT COUNT(*) FROM season WHERE show_id = 's-mkp001'").fetchone()[0] == 3
    # the split-guard review on the season is closed
    assert conn.execute("SELECT resolved_at FROM pending_review").fetchone()[0] is not None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_episodes_stay_with_the_season_and_each_part_counts_its_own(conn):
    from lcars import list_sync

    mp.apply(conn, mp.plan(conn, "s-mkp001", 3, [195516, 200927]))
    part1 = conn.execute("SELECT * FROM season WHERE kind = 'part' AND part_number = 1").fetchone()
    part2 = conn.execute("SELECT * FROM season WHERE kind = 'part' AND part_number = 2").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM episode WHERE season_id = 'z-mkp000'"
                        ).fetchone()[0] == 24
    assert (len(list_sync.level_episodes_ordered(conn, part1)),
            list_sync.level_progress(conn, part1)) == (12, 1)
    assert (len(list_sync.level_episodes_ordered(conn, part2)),
            list_sync.level_progress(conn, part2)) == (12, 0)


def test_running_it_twice_is_refused_not_repeated(conn):
    mp.apply(conn, mp.plan(conn, "s-mkp001", 3, [195516, 200927]))
    with pytest.raises(mp.Refused, match="already has part levels"):
        mp.plan(conn, "s-mkp001", 3, [195516, 200927])


@pytest.mark.parametrize("mutate,message", [
    ("UPDATE season SET anilist_id = 999 WHERE id = 'z-mkp004'", "neither the season nor"),
    ("INSERT INTO episode (id, show_id, season, episode, kind, season_id, created_at,"
     " updated_at) VALUES ('e-mkpx01', 's-mkp001', 4, 1, 'regular', 'z-mkp004', 'x', 'x')",
     "has episodes, spans or children"),
    ("UPDATE season SET episode_total = NULL WHERE id = 'z-mkp000'", "pass --counts"),
])
def test_it_refuses_what_it_cannot_do_safely(conn, mutate, message):
    conn.execute(mutate)
    conn.commit()
    with pytest.raises(mp.Refused, match=message):
        mp.plan(conn, "s-mkp001", 3, [195516, 200927])


def test_counts_can_be_given_when_an_entry_has_no_total(conn):
    conn.execute("UPDATE season SET episode_total = NULL WHERE id = 'z-mkp000'")
    conn.commit()
    p = mp.plan(conn, "s-mkp001", 3, [195516, 200927], counts=[12])
    assert [s["episodes"] for s in p["sources"]] == [12, 12]
