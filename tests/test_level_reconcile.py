"""Levels follow the episodes (level_reconcile.py, level_parts.py): RULEBOOK R1.10, R1.17, R3.6.

Case (2026-10-05): Kusuriya no Hitorigoto S3 — TVDB season 3 has 24 episodes (abs 49-72), AniList
195516 (12) and 200927 (12). The old positional code gave 200927 a fake "S4" row; the episodes
say it is part 2 of season 3."""

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import level_parts
from lcars import level_reconcile as lr

ROOT = Path(__file__).resolve().parent.parent
T = "2026-10-05T00:00:00Z"
TVDB = 431162


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "lr.db"
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
        " VALUES ('s-lr0001', 'episodic', 'anime', 'Show', 'romaji', 'watching', 1, ?, ?)", (T, T))
    c.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-lr0001', 'tvdb', ?, 'x', ?)", (str(TVDB), T))
    c.commit()
    return c


def tvdb_season(conn, sid, number, first_abs, count, start_utc, anilist=None, mal=None,
                status="watching", source="fribb"):
    """A TVDB season with `count` weekly episodes starting at `start_utc`, abs from `first_abs`."""
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, mal_id, status, source,"
        " created_at, updated_at) VALUES (?, 's-lr0001', ?, 'tvdb_season', ?, ?, ?, ?, ?, ?)",
        (sid, number, anilist, mal, status, source, T, T))
    conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                 (sid, first_abs, first_abs + count - 1))
    start = dt.datetime.fromisoformat(start_utc.replace("Z", ""))
    for i in range(count):
        when = (start + dt.timedelta(days=7 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number,"
            " air_date_utc, state, season_id, created_at, updated_at)"
            " VALUES (?, 's-lr0001', ?, ?, 'regular', ?, ?, 'unwatched', ?, ?, ?)",
            (f"e-{number}{i:05d}", number, i + 1, first_abs + i, when, sid, T, T))
    conn.commit()


def leftover(conn, sid, number, anilist, mal=None, status="planned", source="fribb"):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, mal_id, status, source,"
        " created_at, updated_at) VALUES (?, 's-lr0001', ?, 'tvdb_season', ?, ?, ?, ?, ?, ?)",
        (sid, number, anilist, mal, status, source, T, T))
    conn.commit()


def entry(anilist, hint, mal=None, kind="TV"):
    return {"type": kind, "anilist_id": anilist, "mal_id": mal, "tvdb_id": TVDB,
            "season": {"tvdb": hint}}


class Facts:
    """Stands in for AniList: {id: (start date or None, episodes)}; counts the calls."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, ids):
        self.calls.append(list(ids))
        return {i: {"start": self.table[i][0], "episodes": self.table[i][1], "status": "x"}
                for i in ids}


def run(conn, entries, facts, apply=True):
    return lr.reconcile_all(conn, apply=apply, index={TVDB: entries}, facts=facts)


def kusuriya(conn):
    """TVDB S3: 24 episodes from 2026-10-02 (Friday night in Japan = Friday 14:30 UTC)."""
    tvdb_season(conn, "z-s30000", 3, 49, 24, "2026-10-02T14:30:00Z", anilist=195516, mal=61987)
    leftover(conn, "z-s40000", 4, 200927, 62841, status="watching")
    conn.execute("UPDATE season SET status_set_manually = 1 WHERE id = 'z-s40000'")
    conn.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                 " VALUES ('z-s30000', 'anilist', 195516, ?)", (T,))
    conn.commit()
    return ([entry(195516, 3, 61987), entry(200927, 4, 62841)],
            Facts({195516: (dt.date(2026, 10, 2), 12), 200927: (None, 12)}))


def test_the_leftover_season_becomes_part_two_of_the_season_its_episodes_sit_in(conn):
    entries, facts = kusuriya(conn)
    r = run(conn, entries, facts)
    assert r["parts_made"] == 2

    parent = conn.execute("SELECT * FROM season WHERE id = 'z-s30000'").fetchone()
    assert (parent["kind"], parent["anilist_id"], parent["mal_id"]) == ("tvdb_season", None, None)
    parts = conn.execute(
        "SELECT * FROM season WHERE parent_id = 'z-s30000' ORDER BY part_number").fetchall()
    assert [(p["season_number"], p["part_number"], p["anilist_id"], p["mal_id"]) for p in parts] \
        == [(3, 1, 195516, 61987), (3, 2, 200927, 62841)]
    assert parts[1]["id"] == "z-s40000"  # converted in place: id, status, history stay
    assert (parts[1]["status"], parts[1]["status_set_manually"]) == ("watching", 1)
    spans = [conn.execute("SELECT abs_from, abs_to FROM season_span WHERE season_id = ?",
                          (p["id"],)).fetchall() for p in parts]
    assert [[tuple(x) for x in s] for s in spans] == [[(49.0, 60.0)], [(61.0, 72.0)]]
    assert conn.execute("SELECT season_id FROM season_external_id WHERE service = 'anilist'"
                        ).fetchone()[0] == parts[0]["id"]  # the list-id row moved with the id
    assert conn.execute("SELECT COUNT(*) FROM season WHERE show_id = 's-lr0001'").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_settled_show_costs_no_anilist_call_and_changes_nothing(conn):
    entries, facts = kusuriya(conn)
    run(conn, entries, facts)
    before = conn.execute("SELECT id, kind, season_number, part_number FROM season ORDER BY id"
                          ).fetchall()
    facts.calls.clear()
    r = run(conn, entries, facts)
    assert r["parts_made"] == 0 and facts.calls == []
    assert conn.execute("SELECT id, kind, season_number, part_number FROM season ORDER BY id"
                        ).fetchall() == before


def test_a_start_date_is_the_calendar_date_in_japan_not_in_utc(conn):
    # 2026-10-01 16:00 UTC is 2026-10-02 01:00 in Japan: AniList says it started on the 2nd
    tvdb_season(conn, "z-s10000", 1, 1, 12, "2026-10-01T16:00:00Z", anilist=1, status="completed")
    tvdb_season(conn, "z-s20000", 2, 13, 12, "2027-01-01T16:00:00Z")
    r = run(conn, [entry(1, 1), entry(2, 2)],
            Facts({1: (dt.date(2026, 10, 2), 12), 2: (dt.date(2027, 1, 2), 12)}))
    assert r["parts_made"] == 0 and r["linked"] == 1  # entry 2 is season 2's only entry
    assert conn.execute("SELECT anilist_id FROM season WHERE id = 'z-s20000'").fetchone()[0] == 2
    # the strict part: one calendar day off is not a match
    assert lr.jst_date("2026-10-01T16:00:00Z") == dt.date(2026, 10, 2)
    assert lr.jst_date("2026-10-01T14:59:59Z") == dt.date(2026, 10, 1)


def test_an_entry_nobody_holds_becomes_a_planned_part_not_on_the_lists(conn):
    # Attack on Titan shape: TVDB S3 has 22 episodes; the show holds only the first entry (12)
    tvdb_season(conn, "z-s10000", 1, 1, 25, "2013-04-06T15:00:00Z", anilist=16498,
                status="completed")
    tvdb_season(conn, "z-s30000", 3, 43, 22, "2018-07-22T15:00:00Z", anilist=99147, mal=35760)
    facts = Facts({16498: (dt.date(2013, 4, 7), 25), 99147: (dt.date(2018, 7, 23), 12),
                   104578: (dt.date(2019, 4, 30), 10)})
    # 104578 starts on the date of episode 13: 12 weeks after the first
    start = dt.date(2018, 7, 23) + dt.timedelta(weeks=12)
    facts.table[104578] = (start, 10)
    r = run(conn, [entry(16498, 1), entry(99147, 3, 35760), entry(104578, 3, 37521)], facts)
    assert r["parts_made"] == 2
    new = conn.execute("SELECT * FROM season WHERE anilist_id = 104578").fetchone()
    assert (new["kind"], new["season_number"], new["part_number"]) == ("part", 3, 2)
    assert new["list_sync"] == 0 and new["status"] == "planned"
    assert [tuple(x) for x in conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (new["id"],))] \
        == [(55.0, 64.0)]
    assert conn.execute("SELECT mal_id FROM season WHERE id = ?", (new["id"],)).fetchone()[0] \
        == 37521


def test_a_season_tvdb_does_not_have_yet_is_a_season_level_only(conn):
    # SPY x FAMILY shape: TVDB has season 1 only; AniList's second season (hint 2) has no episodes
    tvdb_season(conn, "z-s10000", 1, 1, 12, "2026-04-04T14:00:00Z", anilist=1, status="completed")
    facts = Facts({1: (dt.date(2026, 4, 4), 12), 2: (dt.date(2026, 10, 20), 10)})
    r = run(conn, [entry(1, 1), entry(2, 2, 22)], facts)
    assert r["future_levels"] == 1 and r["parts_made"] == 0
    level = conn.execute("SELECT * FROM season WHERE anilist_id = 2").fetchone()
    assert (level["kind"], level["season_number"], level["status"], level["mal_id"]) \
        == ("tvdb_season", 2, "planned", 22)
    assert run(conn, [entry(1, 1), entry(2, 2, 22)], facts)["future_levels"] == 0  # not twice


def test_a_season_beyond_the_next_one_takes_no_number_it_was_not_given(conn):
    tvdb_season(conn, "z-s10000", 1, 1, 12, "2026-04-04T14:00:00Z", anilist=1, status="completed")
    facts = Facts({1: (dt.date(2026, 4, 4), 12), 3: (dt.date(2027, 4, 1), 10)})
    r = run(conn, [entry(1, 1), entry(3, 3)], facts)  # Fribb says season 3; TVDB's last is 1
    assert r["future_levels"] == 0 and r["left"] == 1
    assert conn.execute("SELECT COUNT(*) FROM season WHERE anilist_id = 3").fetchone()[0] == 0


def test_a_level_the_user_placed_is_never_moved(conn):
    entries, facts = kusuriya(conn)
    conn.execute("UPDATE season SET source = 'manual' WHERE id = 'z-s40000'")
    conn.commit()
    run(conn, entries, facts)
    assert conn.execute("SELECT kind FROM season WHERE id = 'z-s40000'").fetchone()[0] \
        == "tvdb_season"


def test_a_start_date_that_is_not_the_next_free_episode_changes_nothing(conn):
    tvdb_season(conn, "z-s30000", 3, 49, 24, "2026-10-02T14:30:00Z", anilist=195516)
    leftover(conn, "z-s40000", 4, 200927)
    # 200927 claims to start on episode 20's date, but the first cour (12) leaves episode 13 free
    start = dt.date(2026, 10, 2) + dt.timedelta(weeks=19)
    facts = Facts({195516: (dt.date(2026, 10, 2), 12), 200927: (start, 12)})
    r = run(conn, [entry(195516, 3), entry(200927, 4)], facts)
    assert r["parts_made"] == 0 and r["left"] == 1
    assert conn.execute("SELECT kind FROM season WHERE id = 'z-s40000'").fetchone()[0] \
        == "tvdb_season"


def test_a_special_level_holding_a_tv_entry_becomes_its_part(conn):
    # Durarara!!x2 shape: S2 = three cours; the first sits on a special level, the rest on leftovers
    tvdb_season(conn, "z-s20000", 2, 25, 36, "2015-01-09T15:00:00Z")
    conn.execute(
        "INSERT INTO season (id, show_id, kind, anilist_id, status, source, created_at,"
        " updated_at) VALUES ('z-sp0000', 's-lr0001', 'special', 20652, 'planned', 'manual', ?, ?)",
        (T, T))
    leftover(conn, "z-s30000", 3, 20879)
    leftover(conn, "z-s40000", 4, 20880)
    d = dt.date(2015, 1, 10)
    facts = Facts({20652: (d, 12), 20879: (d + dt.timedelta(weeks=12), 12),
                   20880: (d + dt.timedelta(weeks=24), 12)})
    r = run(conn, [entry(20652, 2), entry(20879, 2), entry(20880, 2)], facts)
    assert r["parts_made"] == 3
    got = conn.execute(
        "SELECT id, kind, season_number, part_number, anilist_id FROM season WHERE parent_id ="
        " 'z-s20000' ORDER BY part_number").fetchall()
    assert [(g["id"], g["kind"], g["season_number"], g["part_number"]) for g in got] == [
        ("z-sp0000", "part", 2, 1), ("z-s30000", "part", 2, 2), ("z-s40000", "part", 2, 3)]


def test_without_a_start_date_the_entries_fit_by_count_in_release_order(conn):
    # Hozuki shape: S2 = 26 episodes, two cours of 13; the first one's date matches no episode
    tvdb_season(conn, "z-s20000", 2, 14, 26, "2017-10-06T16:00:00Z")
    leftover(conn, "z-s30000", 3, 100852)
    conn.execute(
        "INSERT INTO season (id, show_id, kind, anilist_id, status, source, created_at,"
        " updated_at) VALUES ('z-sp0000', 's-lr0001', 'special', 98438, 'planned', 'manual', ?, ?)",
        (T, T))
    second = dt.date(2017, 10, 7) + dt.timedelta(weeks=13)
    facts = Facts({98438: (dt.date(2017, 9, 1), 13), 100852: (second, 13)})
    r = run(conn, [entry(98438, 2), entry(100852, 2)], facts)
    assert r["parts_made"] == 2
    assert [tuple(x) for x in conn.execute(
        "SELECT anilist_id, part_number FROM season WHERE parent_id = 'z-s20000' ORDER BY"
        " part_number")] == [(98438, 1), (100852, 2)]


def test_make_part_by_hand_marks_the_level_so_it_stays(conn, monkeypatch):
    tvdb_season(conn, "z-s30000", 3, 49, 24, "2026-10-02T14:30:00Z", anilist=195516, mal=61987)
    leftover(conn, "z-s40000", 4, 200927, 62841)
    facts = Facts({195516: (dt.date(2026, 10, 3), 12), 200927: (None, 12)})
    monkeypatch.setattr(lr, "facts_for", facts)
    part_id = lr.make_part(conn, "z-s40000", 3)
    assert part_id == "z-s40000"
    got = conn.execute("SELECT kind, source, manual_override, part_number FROM season WHERE id ="
                       " 'z-s40000'").fetchone()
    assert tuple(got) == ("part", "manual", 1, 2)
    assert conn.execute("SELECT COUNT(*) FROM season WHERE parent_id = 'z-s30000'").fetchone()[0] \
        == 2


def test_make_part_by_hand_says_why_it_cannot(conn, monkeypatch):
    tvdb_season(conn, "z-s30000", 3, 49, 12, "2026-10-02T14:30:00Z", anilist=195516)
    leftover(conn, "z-s40000", 4, 200927)
    monkeypatch.setattr(lr, "facts_for", Facts({195516: (None, 12), 200927: (None, 12)}))
    with pytest.raises(level_parts.Refused, match="no TVDB season 9"):
        lr.make_part(conn, "z-s40000", 9)
    with pytest.raises(level_parts.Refused, match="no free episode"):
        lr.make_part(conn, "z-s40000", 3)  # the season's 12 episodes are the parent's own entry


def test_a_leftover_level_that_is_the_seasons_only_entry_hands_it_everything(conn):
    tvdb_season(conn, "z-s20000", 2, 13, 12, "2025-01-10T15:00:00Z", status="planned")
    leftover(conn, "z-s30000", 3, 555, 777, status="completed", source="fribb")
    conn.execute("UPDATE season SET status_set_manually = 1, score = 8 WHERE id = 'z-s30000'")
    conn.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                 " VALUES ('z-s30000', 'anilist', 555, ?)", (T,))
    conn.commit()
    r = run(conn, [entry(555, 2, 777)], Facts({555: (dt.date(2025, 1, 11), 12)}))
    assert r["linked"] == 1
    got = conn.execute("SELECT anilist_id, mal_id, status, status_set_manually, score FROM season"
                       " WHERE id = 'z-s20000'").fetchone()
    assert tuple(got) == (555, 777, "completed", 1, 8)
    assert conn.execute("SELECT COUNT(*) FROM season WHERE id = 'z-s30000'").fetchone()[0] == 0
    assert conn.execute("SELECT season_id FROM season_external_id").fetchone()[0] == "z-s20000"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cours_whose_dates_miss_but_whose_counts_fill_the_season_are_its_parts(conn):
    # Dungeon S4 shape: 22 episodes; AniList's two cours (11 + 11) start two days off TVDB's dates
    tvdb_season(conn, "z-s10000", 1, 1, 13, "2015-04-03T15:00:00Z", anilist=1, status="completed")
    tvdb_season(conn, "z-s40000", 4, 14, 22, "2022-07-23T15:00:00Z", status="planned")
    facts = Facts({1: (dt.date(2015, 4, 4), 13), 129196: (dt.date(2022, 7, 21), 11),
                   155211: (dt.date(2023, 1, 5), 11)})
    r = run(conn, [entry(1, 1), entry(129196, 4), entry(155211, 4)], facts)
    assert r["parts_made"] == 2 and r["left"] == 0
    assert [tuple(x) for x in conn.execute(
        "SELECT anilist_id, part_number FROM season WHERE parent_id = 'z-s40000'"
        " ORDER BY part_number")] == [(129196, 1), (155211, 2)]


def test_cours_that_do_not_fill_the_season_stay_unplaced(conn):
    tvdb_season(conn, "z-s10000", 1, 1, 13, "2015-04-03T15:00:00Z", anilist=1, status="completed")
    tvdb_season(conn, "z-s40000", 4, 14, 24, "2022-07-23T15:00:00Z", status="planned")
    facts = Facts({1: (dt.date(2015, 4, 4), 13), 129196: (dt.date(2022, 7, 21), 11),
                   155211: (dt.date(2023, 1, 5), 11)})
    r = run(conn, [entry(1, 1), entry(129196, 4), entry(155211, 4)], facts)
    assert r["parts_made"] == 0 and r["left"] == 2  # 11 + 11 of 24: something is missing


def test_the_weekly_season_mapping_does_not_give_a_divided_season_its_first_cours_id_back(
    conn, monkeypatch,
):
    from lcars import fribb, season_mapping

    entries, facts = kusuriya(conn)
    run(conn, entries, facts)
    monkeypatch.setattr(fribb, "load_dataset", lambda *a, **k: entries)  # Fribb: S3 = 195516
    season_mapping.reconcile_season(conn, "s-lr0001", 3)
    parent = conn.execute("SELECT anilist_id, mal_id FROM season WHERE id = 'z-s30000'").fetchone()
    assert tuple(parent) == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM season WHERE anilist_id = 195516").fetchone()[0] == 1


# ── 2026-10-10: With Vengeance, Sincerely, Your Broken Saintess ──────────────
# TVDB S1 and S2 (12 episodes each); AniList 195209 and 212144; Fribb lists only 195209. The add
# check had made "part 2 of S1" for 212144 (empty, source auto) and S1 was divided into an empty
# part 1. Nothing placed 212144 for five days: the reconciler only looked at Fribb's entries.


def vengeance(conn):
    tvdb_season(conn, "z-v10000", 1, 1, 12, "2025-07-09T15:30:00Z")
    tvdb_season(conn, "z-v20000", 2, 13, 12, "2026-10-01T15:00:00Z", status="watching")
    for zid, part, aid, mal in (("z-vp1000", 1, 195209, 59961), ("z-vp2000", 2, 212144, 64180)):
        conn.execute(
            "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
            " anilist_id, mal_id, status, source, created_at, updated_at) VALUES (?, 's-lr0001',"
            " 1, ?, 'part', 'z-v10000', ?, ?, 'planned', 'auto', ?, ?)",
            (zid, part, aid, mal, T, T))
    conn.commit()
    return ([entry(195209, 1, 59961)],  # Fribb knows the first entry only
            Facts({195209: (dt.date(2025, 7, 10), 12), 212144: (dt.date(2026, 10, 2), 12)}))


def test_an_entry_fribb_does_not_list_is_placed_by_its_episodes_from_the_part_that_holds_it(conn):
    entries, facts = vengeance(conn)
    r = run(conn, entries, facts)
    assert (r["linked"], r["parts_made"], r["left"]) == (2, 0, 0)
    held = {row["season_number"]: (row["kind"], row["anilist_id"], row["mal_id"])
            for row in conn.execute("SELECT * FROM season WHERE kind = 'tvdb_season'")}
    assert held == {1: ("tvdb_season", 195209, 59961), 2: ("tvdb_season", 212144, 64180)}
    assert conn.execute("SELECT COUNT(*) FROM season WHERE kind = 'part'").fetchone()[0] == 0


def test_a_second_pass_changes_nothing(conn):
    entries, facts = vengeance(conn)
    run(conn, entries, facts)
    again = run(conn, entries, facts)
    assert (again["linked"], again["parts_made"], again["left"], again["shows"]) == (0, 0, 0, 0)


def test_a_held_entry_is_placed_by_a_start_date_never_by_a_count_alone(conn):
    entries, _ = vengeance(conn)
    undated = Facts({195209: (dt.date(2025, 7, 10), 12), 212144: (None, 12)})
    r = run(conn, entries, undated)
    assert r["linked"] == 1  # 195209 only
    assert conn.execute("SELECT parent_id FROM season WHERE id = 'z-vp2000'").fetchone()[0] \
        == "z-v10000"  # 212144 stays where it was: reported, not guessed
    assert r["plan"][0]["left"] == [(212144, "no episode of the show aired on its start date")]


def test_a_part_you_placed_by_hand_is_never_moved(conn):
    entries, facts = vengeance(conn)
    conn.execute("UPDATE season SET source = 'manual' WHERE id = 'z-vp2000'")
    conn.commit()
    run(conn, entries, facts)
    assert conn.execute("SELECT kind FROM season WHERE id = 'z-vp2000'").fetchone()[0] == "part"


def test_a_part_with_spans_is_a_settled_part_and_stays(conn):
    entries, facts = vengeance(conn)
    conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to)"
                 " VALUES ('z-vp2000', 13, 24)")
    conn.commit()
    run(conn, entries, facts)
    assert conn.execute("SELECT kind, parent_id FROM season WHERE id = 'z-vp2000'").fetchone()[:] \
        == ("part", "z-v10000")


def test_a_part_inside_a_season_you_placed_by_hand_is_your_structure_and_stays(conn):
    entries, facts = vengeance(conn)
    conn.execute("UPDATE season SET source = 'manual', manual_override = 1 WHERE id = 'z-v10000'")
    conn.commit()
    r = run(conn, entries, facts)
    assert conn.execute("SELECT kind FROM season WHERE id = 'z-vp2000'").fetchone()[0] == "part"
    assert r["linked"] == 0 or r["plan"][0]["left"] == []  # nothing inside S1 was moved
