"""Memory Alpha numbering engine (PLAN-CODE phase 3.2) — RULEBOOK R1.0–R1.12, R1.2a–d."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import numbering
from lcars.numbering import Item

NOW = "2026-01-01T00:00:00Z"


def _ep(eid, s, e, day, **kw):
    return Item(eid, s, e, f"2020-01-{day:02d}T12:00:00Z" if day else None, **kw)


def _two_seasons(extra=()):
    # S1: days 1-2; S2: days 10-13.
    return [
        _ep("a1", 1, 1, 1), _ep("a2", 1, 2, 2),
        _ep("b1", 2, 1, 10), _ep("b2", 2, 2, 11), _ep("b3", 2, 3, 12), _ep("b4", 2, 4, 13),
        *extra,
    ]


def _plan(items, source="tvdb", **kw):
    return numbering.plan_show("s-test01", items, source, **kw)


def _kinds(plan):
    return [f["kind"] for f in plan.flags]


def test_main_episodes_run_whole_numbers_and_each_season_is_its_span():
    plan = _plan(_two_seasons())
    assert [plan.numbers[i] for i in ("a1", "a2", "b1", "b4")] == [1, 2, 3, 6]
    assert plan.season_spans == {1: [(1, 2)], 2: [(3, 6)]}
    assert plan.flags == []


def test_film_mid_season_takes_a_whole_number_and_splits_the_span():
    # R1.12's example: the season is two spans around the film.
    film = _ep("f1", 0, 1, 11, runtime=100)
    film.air = "2020-01-11T20:00:00Z"
    plan = _plan(_two_seasons([film]))
    assert plan.numbers["f1"] == 5
    assert [plan.numbers[i] for i in ("b2", "b3", "b4")] == [4, 6, 7]
    assert plan.season_spans[2] == [(3, 4), (6, 7)]
    assert "film_placement" in _kinds(plan)  # R1.4/R1.5: for the user to confirm
    assert "no_level" in _kinds(plan)


def test_one_special_inside_a_season_is_n_point_5_and_not_in_the_season():
    sp = _ep("x1", 0, 1, 1)
    sp.air = "2020-01-01T20:00:00Z"
    plan = _plan(_two_seasons([sp]))
    assert plan.numbers["x1"] == 1.5  # R1.2b: one item alone → .5
    assert plan.numbers["a2"] == 2
    assert plan.season_spans[1] == [(1, 1), (2, 2)]  # R2.7: not counted in S1


def test_several_specials_in_one_gap_are_point_1_point_2_in_air_order():
    later = _ep("x2", 0, 1, 1)
    later.air = "2020-01-01T22:00:00Z"
    earlier = _ep("x1", 0, 2, 1)
    earlier.air = "2020-01-01T20:00:00Z"
    plan = _plan(_two_seasons([later, earlier]))
    assert (plan.numbers["x1"], plan.numbers["x2"]) == (1.1, 1.2)


def test_ova_between_seasons_takes_a_whole_number_and_shifts_later_ones():
    ova = _ep("o1", 0, 1, 5)
    plan = _plan(_two_seasons([ova]))
    assert plan.numbers["o1"] == 3  # R1.8b
    assert plan.numbers["b1"] == 4
    assert plan.season_spans == {1: [(1, 2)], 2: [(4, 7)]}


def test_item_before_episode_1_is_zero_point_5():
    # Frieren: the pre-air block shown as a film → 0.5 (R1.2b, R1.5).
    film = Item("f0", 0, 1, "2019-12-30T12:00:00Z", runtime=120)
    plan = _plan(_two_seasons([film]))
    assert plan.numbers["f0"] == 0.5
    assert plan.numbers["a1"] == 1
    assert "film_placement" in _kinds(plan)


def test_special_after_the_last_episode_takes_a_whole_number():
    sp = _ep("z1", 0, 1, 20)
    plan = _plan(_two_seasons([sp]))
    assert plan.numbers["z1"] == 7


def test_tv_christmas_special_between_seasons_is_whole():
    xmas = Item("c1", 0, 1, "2020-01-05T20:00:00Z", runtime=45)
    plan = _plan(_two_seasons([xmas]), source="tvmaze")
    assert plan.numbers["c1"] == 3


def test_special_without_air_date_is_left_unnumbered_and_listed():
    plan = _plan(_two_seasons([Item("n1", 0, 1, None)]))
    assert plan.numbers["n1"] is None
    assert "no_air_date" in _kinds(plan)


def test_ten_specials_in_one_gap_use_hundredths():
    extra = []
    for i in range(10):
        it = _ep(f"m{i}", 0, i + 1, 1)
        it.air = f"2020-01-01T{13 + i:02d}:00:00Z"
        extra.append(it)
    plan = _plan(_two_seasons(extra))
    assert plan.numbers["m0"] == 1.01 and plan.numbers["m9"] == 1.1
    assert plan.numbers["a2"] == 2
    assert "ten_or_more_in_one_gap" in _kinds(plan)


def test_anidb_special_inside_a_tvdb_season_becomes_a_decimal():
    items = _two_seasons()
    items[3].anidb = (500, 0, 1)  # b2: AniDB files it as a special (R1.3, R1.8c)
    plan = _plan(items, source="anidb")
    assert plan.numbers["b2"] == 3.5
    assert plan.numbers["b3"] == 4
    assert "anidb_special_in_tvdb_season" in _kinds(plan)


def test_anidb_regular_in_season_0_joins_the_main_order():
    items = _two_seasons()
    for it in items:
        it.anidb = (100, 1, it.tvdb_episode) if it.tvdb_season == 1 else (200, 1, it.tvdb_episode)
    ep13 = _ep("s0", 0, 1, 14, anidb=(200, 1, 5))  # AniDB: episode 5 of the S2 entry
    items.append(ep13)
    plan = _plan(items, source="anidb", main_anidb_ids={100, 200})
    assert plan.numbers["s0"] == 7
    assert plan.season_spans[2] == [(3, 7)]
    assert "anidb_regular_in_season_0" in _kinds(plan)


def test_parts_of_a_tvdb_season_get_their_own_spans():
    items = _two_seasons()
    for it in items:
        if it.tvdb_season == 2:
            it.anidb = (300 if it.tvdb_episode <= 2 else 301, 1, it.tvdb_episode)
    plan = _plan(items, source="anidb")
    assert plan.level_spans["part:2:300"] == [(3, 4)]
    assert plan.level_spans["part:2:301"] == [(5, 6)]


def test_side_item_with_its_own_anidb_entry_is_a_level():
    ova = _ep("o1", 0, 1, 5, anidb=(900, 1, 1))
    plan = _plan(_two_seasons([ova]), source="anidb")
    assert plan.level_spans["anidb:900"] == [(3, 3)]
    assert "no_level" not in _kinds(plan)


def test_order_disagreement_is_settled_by_air_date():
    items = _two_seasons()
    # AniDB says b2 comes after b3; air dates agree with AniDB.
    items[3].anidb, items[4].anidb = (300, 1, 3), (300, 1, 2)
    items[2].anidb, items[5].anidb = (300, 1, 1), (300, 1, 4)
    items[3].air, items[4].air = "2020-01-12T13:00:00Z", "2020-01-11T12:00:00Z"
    plan = _plan(items, source="anidb")
    assert plan.numbers["b3"] == 4 and plan.numbers["b2"] == 5
    assert "order_by_air_date" in _kinds(plan)


def test_same_input_same_plan():
    film = _ep("f1", 0, 1, 11, runtime=100)
    assert _plan(_two_seasons([film])) == _plan(_two_seasons([film]))


# ── writer ─────────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "numbering.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, created_at, updated_at)"
        " VALUES ('s-test01', 'episodic', 'tv', 'Show', 'english', 'watching', ?, ?)",
        (NOW, NOW),
    )
    for zid, n in (("z-test01", 1), ("z-test02", 2)):
        c.execute(
            "INSERT INTO season (id, show_id, season_number, source, created_at, updated_at)"
            " VALUES (?, 's-test01', ?, 'manual', ?, ?)",
            (zid, n, NOW, NOW),
        )
    for it in _two_seasons([_ep("x1", 0, 1, 1)]):
        c.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc,"
            " created_at, updated_at) VALUES (?, 's-test01', ?, ?, 'regular', ?, ?, ?)",
            (f"e-{it.id}0000"[:8], it.tvdb_season, it.tvdb_episode,
             it.air.replace("12:00", "20:00") if it.tvdb_season == 0 else it.air, NOW, NOW),
        )
    c.commit()
    yield c
    c.close()


def test_writer_sets_numbers_spans_and_source_and_logs_reconciliations(conn):
    plan = numbering.renumber_show(conn, "s-test01")
    assert plan.source == "tvdb"  # no TVmaze data: the R1.2d fallback
    got = dict(conn.execute("SELECT season || ':' || episode, absolute_number FROM episode"))
    assert got["0:1"] == 1.5 and got["2:1"] == 3
    spans = conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = 'z-test01' ORDER BY abs_from"
    ).fetchall()
    assert spans == [(1, 1), (2, 2)]
    assert conn.execute(
        "SELECT abs_start, abs_end FROM season WHERE id = 'z-test01'"
    ).fetchone() == (1, 2)
    assert conn.execute(
        "SELECT absolute_numbering_source FROM show WHERE id = 's-test01'"
    ).fetchone() == ("tvdb",)
    # First numbering is not a reconciliation: nothing logged.
    assert conn.execute("SELECT COUNT(*) FROM absolute_number_change").fetchone() == (0,)

    # A special found later between the seasons shifts S2: logged for review.
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc,"
        " created_at, updated_at)"
        " VALUES ('e-late01', 's-test01', 0, 2, 'special', '2020-01-05T12:00:00Z', ?, ?)",
        (NOW, NOW),
    )
    numbering.renumber_show(conn, "s-test01")
    changes = conn.execute("SELECT COUNT(*) FROM absolute_number_change").fetchone()[0]
    assert changes == 5  # the new special + S2's four episodes
    numbering.renumber_show(conn, "s-test01")
    assert conn.execute("SELECT COUNT(*) FROM absolute_number_change").fetchone()[0] == changes
