"""rulecheck (PLAN-CODE phase 1): read-only check of a database against RULEBOOK.md."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import rulecheck

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "rulecheck.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    return path


def _write(path, sql, params=()):
    c = sqlite3.connect(path)
    c.execute(sql, params)
    c.commit()
    c.close()


def _show(path, sid, title, status="completed", tracked=1, tvdb="100"):
    _write(
        path,
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'english', ?, ?, ?, ?)",
        (sid, title, status, tracked, NOW, NOW),
    )
    if tvdb:
        _write(
            path,
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tvdb', ?, '', ?)",
            (sid, tvdb, NOW),
        )


def _season(path, zid, sid, n, status, start, end):
    _write(
        path,
        "INSERT INTO season (id, show_id, season_number, source, status, abs_start,"
        " abs_end, created_at, updated_at) VALUES (?, ?, ?, 'manual', ?, ?, ?, ?, ?)",
        (zid, sid, n, status, start, end, NOW, NOW),
    )
    if start is not None and end is not None:  # spans: set by Memory Alpha (phase 3.2)
        _write(
            path,
            "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
            (zid, start, end),
        )


def _episode(path, eid, sid, zid, season, ep, abs_n, state="watched"):
    _write(
        path,
        "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number,"
        " air_date_utc, air_date_source, state, season_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, '2020-01-01T00:00:00Z', 'sonarr', ?, ?, ?, ?)",
        (eid, sid, season, ep, abs_n, state, zid, NOW, NOW),
    )


def _by_rule(path):
    return {f.rule: f for f in rulecheck.run(rulecheck.open_readonly(str(path)))}


def test_clean_database_has_no_violation(db_path):
    _show(db_path, "s-aaaaaa", "Clean Show")
    _season(db_path, "z-aaaaaa", "s-aaaaaa", 1, "completed", 1, 2)
    _episode(db_path, "e-aaaaa1", "s-aaaaaa", "z-aaaaaa", 1, 1, 1)
    _episode(db_path, "e-aaaaa2", "s-aaaaaa", "z-aaaaaa", 1, 2, 2)
    found = rulecheck.run(rulecheck.open_readonly(str(db_path)))
    assert [f.rule for f in found if f.kind == "violation" and f.count] == []


def test_planted_violations_are_found(db_path):
    _show(db_path, "s-bbbbbb", "Broken Show", status="watching")
    _show(db_path, "s-cccccc", "Duplicate Of It", tvdb="100")
    _show(db_path, "s-dddddd", "Stub", status="planned", tracked=0, tvdb=None)  # R3.5
    _season(db_path, "z-bbbbb1", "s-bbbbbb", 1, "completed", 1, 2)
    _season(db_path, "z-bbbbb2", "s-bbbbbb", 2, "planned", 3, 3)
    _episode(db_path, "e-bbbbb1", "s-bbbbbb", "z-bbbbb1", 1, 1, 1)
    _episode(db_path, "e-bbbbb2", "s-bbbbbb", "z-bbbbb1", 1, 2, 2, state="unwatched")  # R2.7
    _episode(db_path, "e-bbbbb4", "s-bbbbbb", None, 9, 9, None, state="unwatched")  # R1.0
    _episode(db_path, "e-bbbbb3", "s-bbbbbb", None, 0, 1, 1.1)  # R1.8: season 0, no season link
    found = _by_rule(db_path)
    assert found["R1.0"].count == 1
    assert found["R1.8"].count == 1
    assert found["R2.7"].count == 1
    assert found["R2.13"].count == 1  # show watching, last season planned
    assert found["R3.5"].count == 1
    assert found["R1.14"].count == 1  # both tracked shows carry tvdb 100


def test_database_is_opened_read_only(db_path):
    conn = rulecheck.open_readonly(str(db_path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM show")


def test_cli_exit_code(db_path, capsys):
    assert rulecheck.main([str(db_path)]) == 0
    _show(db_path, "s-eeeeee", "Stub", status="planned", tracked=0, tvdb=None)
    assert rulecheck.main([str(db_path)]) == 1
    assert "R3.5" in capsys.readouterr().out


def _level(path, zid, sid, n, spans, kind="tvdb_season", parent=None, part=1):
    _write(
        path,
        "INSERT INTO season (id, show_id, season_number, part_number, source, status, kind,"
        " parent_id, decimal_season_number, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 'planned', ?, ?, ?, ?, ?)",
        (zid, sid, None if kind == "special" else int(n), part, kind, parent, n, NOW, NOW),
    )
    for a, b in spans:
        _write(
            path,
            "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
            (zid, a, b),
        )


def test_levels_parts_inside_their_season_are_not_overlaps(db_path):
    # R1.12 worked example: film abs 13 between S1 and S2; S2 = part 1 + part 2.
    _show(db_path, "s-ffffff", "Levels", status="planned")
    _level(db_path, "z-fffff1", "s-ffffff", 1, [(1, 12)])
    _level(db_path, "z-fffff2", "s-ffffff", 1.5, [(13, 13)], kind="special")
    _level(db_path, "z-fffff3", "s-ffffff", 2, [(14, 30)])
    _level(db_path, "z-fffff4", "s-ffffff", 2, [(14, 26)], kind="part", parent="z-fffff3")
    _level(db_path, "z-fffff5", "s-ffffff", 2, [(27, 30)], "part", "z-fffff3", part=2)
    found = _by_rule(db_path)
    assert found["R1.12"].count == 0
    assert found["R1.10"].count == 0


def test_levels_overlaps_and_stray_parts_are_found(db_path):
    _show(db_path, "s-gggggg", "Broken Levels", status="planned")
    _level(db_path, "z-ggggg1", "s-gggggg", 1, [(1, 12)])
    _level(db_path, "z-ggggg2", "s-gggggg", 2, [(12, 24)])  # shares abs 12 with S1
    _level(db_path, "z-ggggg3", "s-gggggg", 2, [(13, 18)], kind="part", parent="z-ggggg2")
    _level(db_path, "z-ggggg4", "s-gggggg", 2, [(17, 26)], "part", "z-ggggg2", part=2)
    found = _by_rule(db_path)
    assert found["R1.12"].count == 2  # S1 × S2, part 1 × part 2
    assert found["R1.10"].count == 1  # part 2 runs past S2's end


def test_a_films_tvdb_movie_id_is_its_tvdb_link(db_path):
    _show(db_path, "s-film01", "A Film", tvdb=None)
    _show(db_path, "s-none01", "No Link", tvdb=None)
    _write(db_path, "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
           " VALUES ('s-film01', 'tvdb_movie', '9702', '', ?)", (NOW,))
    conn = rulecheck.open_readonly(str(db_path))
    finding = next(f for f in rulecheck.run(conn) if f.rule == "R3.2")
    assert finding.count == 1 and "No Link" in finding.samples[0]


def _watched_level(db_path, total, status="watching"):
    _show(db_path, "s-r215a1", "Airing Show", status="watching", tvdb="900")
    _write(db_path, "INSERT INTO season (id, show_id, season_number, source, status, kind,"
           " episode_total, anilist_id, created_at, updated_at) VALUES ('z-r215a1', 's-r215a1', 1,"
           " 'manual', ?, 'tvdb_season', ?, 5, ?, ?)", (status, total, NOW, NOW))
    for n in (1, 2):
        _write(db_path, "INSERT INTO episode (id, show_id, season, episode, kind, state,"
               " absolute_number, created_at, updated_at) VALUES (?, 's-r215a1', 1, ?, 'regular',"
               " 'watched', ?, ?, ?)", (f"e-r215a{n}", n, n, NOW, NOW))


def test_r215_an_unconfirmed_count_is_not_a_violation(db_path):
    _watched_level(db_path, total=None)  # an airing season: no total yet (R2.15a)
    finding = next(f for f in rulecheck.run(rulecheck.open_readonly(str(db_path)))
                   if f.rule == "R2.15")
    assert finding.count == 0


def test_r215_a_confirmed_count_that_is_not_completed_is_one(db_path):
    _watched_level(db_path, total=2)
    finding = next(f for f in rulecheck.run(rulecheck.open_readonly(str(db_path)))
                   if f.rule == "R2.15")
    assert finding.count == 1 and "Airing Show S1" in finding.samples[0]


def test_r27_counts_a_levels_own_episodes_not_every_episode_of_its_tvdb_season(db_path):
    # K-ON! S1: episode 12.1 carries TVDB season 1 but is another level's (R1.13a)
    _show(db_path, "s-konkon", "K-ON")
    _season(db_path, "z-konkon", "s-konkon", 1, "completed", None, None)
    _write(db_path, "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES"
           " ('z-konkon', 1, 1), ('z-konkon', 2, 2)")  # broken around the special (R1.12)
    _episode(db_path, "e-kon001", "s-konkon", "z-konkon", 1, 1, 1)
    _episode(db_path, "e-kon002", "s-konkon", "z-konkon", 1, 2, 2)
    _episode(db_path, "e-kon003", "s-konkon", None, 1, 3, 1.5, state="unwatched")
    found = _by_rule(db_path)
    assert found["R2.7"].count == 0


# ── 2026-10-05: the six checks added after the audit ──────────────────────────────────────


def _lvl(path, zid, sid, kind, label=None, parent=None, part=1, anilist=None, spans=(),
           number=None):
    _write(
        path,
        "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id, label,"
        " anilist_id, source, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', 'planned', ?, ?)",
        (zid, sid, number, part, kind, parent, label, anilist, NOW, NOW),
    )
    for lo, hi in spans:
        _write(path, "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
               (zid, lo, hi))


def _ep(path, eid, sid, season, ep, abs_n, date="2020-01-01T00:00:00Z", source="sonarr"):
    _write(
        path,
        "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number, air_date_utc,"
        " air_date_source, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, ?, ?, 'unwatched', ?, ?)",
        (eid, sid, season, ep, abs_n, date, source if date else None, NOW, NOW),
    )


def test_r113b_a_special_level_copied_every_pass_is_found(db_path):
    _show(db_path, "s-dupl01", "Pieces")
    _season(db_path, "z-dupl00", "s-dupl01", 1, "watching", 1, 2)
    for i in range(3):  # the runaway: three id-less copies of one piece under one group
        _lvl(db_path, f"z-dupl{i + 1:02d}", "s-dupl01", "special", "S00E03 Podcast 3",
               parent="z-dupl00")
    _lvl(db_path, "z-dupl09", "s-dupl01", "special", "S00E04 Podcast 4", parent="z-dupl00")
    found = _by_rule(db_path)["R1.13b"]
    assert found.count == 1 and "× 3" in found.samples[0]


def test_r113b_a_twin_beside_a_level_that_holds_an_id_is_not_a_duplicate(db_path):
    _show(db_path, "s-twin01", "Twins")
    _lvl(db_path, "z-twin01", "s-twin01", "special", "Side piece after season 2", anilist=204431)
    _lvl(db_path, "z-twin02", "s-twin01", "special", "Side piece after season 2")
    first = [f for f in rulecheck.run(rulecheck.open_readonly(str(db_path)))
             if f.title.startswith("A special level exists once")][0]
    assert first.count == 0


def test_r113b_two_sibling_levels_holding_one_episode_are_listed_to_look_at(db_path):
    _show(db_path, "s-both01", "Overlap")
    _season(db_path, "z-both00", "s-both01", 1, "watching", 1, 5)
    _lvl(db_path, "z-both01", "s-both01", "special", "Season 1 minis", parent="z-both00",
           spans=[(3.5, 3.5)])
    _lvl(db_path, "z-both02", "s-both01", "special", "The Film", parent="z-both00",
           spans=[(3.5, 3.5)], anilist=77)
    _lvl(db_path, "z-both03", "s-both01", "part", "Part", parent="z-both00", part=2,
           spans=[(1, 2)])  # a child inside its season: not a double
    _ep(db_path, "e-both01", "s-both01", 0, 1, 3.5)
    _ep(db_path, "e-both02", "s-both01", 1, 1, 1)
    found = _by_rule(db_path)["R1.13c"]
    assert found.kind == "check" and found.count == 1 and "in 2 levels" in found.samples[0]


def test_r12b_decimal_numbers_follow_the_scheme(db_path):
    _show(db_path, "s-deci01", "Decimals")
    # 1.5 alone: ok · 2.1, 2.2: ok · 3.3 alone: wrong (a single item is .5)
    for i, n in enumerate([1, 1.5, 2, 2.1, 2.2, 3, 3.3]):
        _ep(db_path, f"e-deci{i:02d}", "s-deci01", 1, i + 1, n)
    found = _by_rule(db_path)["R1.2b"]
    assert found.count == 1 and "after 3" in found.samples[0]


def test_r12b_ten_items_in_one_gap_take_hundredths(db_path):
    _show(db_path, "s-hund01", "Hundredths")
    _ep(db_path, "e-hund00", "s-hund01", 1, 1, 1)
    for i in range(10):
        _ep(db_path, f"e-hund{i + 1:02d}", "s-hund01", 0, i + 1, round(1 + (i + 1) / 100, 2))
    assert _by_rule(db_path)["R1.2b"].count == 0


def test_r10a_a_placeholder_number_on_a_dated_episode_is_a_violation(db_path):
    _show(db_path, "s-plac01", "Placeholders")
    _ep(db_path, "e-plac01", "s-plac01", 1, 1, 5000.1)  # dated: wrong
    _ep(db_path, "e-plac02", "s-plac01", 1, 2, 5000.2, date=None)  # undated placeholder: right
    _ep(db_path, "e-plac03", "s-plac01", 1, 3, 3, date=None)  # undated with a real number: look
    found = {f.rule: f for f in rulecheck.run(rulecheck.open_readonly(str(db_path)))}
    assert found["R1.0a"].count == 1 and found["R1.0a"].kind == "violation"
    assert found["R1.0a-b"].count == 1 and found["R1.0a-b"].kind == "check"


def test_r110_parts_are_numbered_in_span_order(db_path):
    _show(db_path, "s-part01", "Parts")
    _season(db_path, "z-part00", "s-part01", 4, "watching", 58, 94)
    _lvl(db_path, "z-part01", "s-part01", "part", parent="z-part00", part=1, spans=[(82, 94)])
    _lvl(db_path, "z-part02", "s-part01", "part", parent="z-part00", part=2, spans=[(58, 69)])
    _lvl(db_path, "z-part03", "s-part01", "part", parent="z-part00", part=3, spans=[(70, 81)])
    found = _by_rule(db_path)["R1.10b"]
    assert found.count == 1 and "S4" in found.samples[0]


def test_r110_parts_in_order_or_without_a_span_are_not_flagged(db_path):
    _show(db_path, "s-part11", "Fine Parts")
    _season(db_path, "z-part10", "s-part11", 1, "watching", 1, 20)
    _lvl(db_path, "z-part11", "s-part11", "part", parent="z-part10", part=1, spans=[(1, 10)])
    _lvl(db_path, "z-part12", "s-part11", "part", parent="z-part10", part=2, spans=[(11, 20)])
    _show(db_path, "s-part21", "Unplaced Parts")
    _season(db_path, "z-part20", "s-part21", 1, "watching", 1, 20)
    _lvl(db_path, "z-part21", "s-part21", "part", parent="z-part20", part=1, spans=[(11, 20)])
    _lvl(db_path, "z-part22", "s-part21", "part", parent="z-part20", part=2)  # no span yet
    found = [f for f in rulecheck.run(rulecheck.open_readonly(str(db_path)))
             if f.title.startswith("Parts are numbered")][0]
    assert found.count == 0


def test_r10b_a_dated_episode_without_a_source_is_a_violation(db_path):
    _show(db_path, "s-srce01", "Sources")
    _ep(db_path, "e-srce01", "s-srce01", 1, 1, 1, source=None)
    _ep(db_path, "e-srce02", "s-srce01", 1, 2, 2)
    assert _by_rule(db_path)["R1.0b"].count == 1


def test_r110a_a_level_with_a_list_id_and_no_episode_is_found_unless_it_is_tvdbs_next_season(
    db_path,
):
    _show(db_path, "s-lvl001", "Follows Episodes")
    _season(db_path, "z-lvl001", "s-lvl001", 1, "completed", 1, 12)
    _ep(db_path, "e-lvl001", "s-lvl001", 1, 1, 1)
    # S2 holds an entry and has no episode: TVDB's next season, a season not there yet — fine
    _lvl(db_path, "z-lvl002", "s-lvl001", "tvdb_season", number=2, anilist=222)
    assert _by_rule(db_path)["R1.10a"].count == 0
    # S4 beyond that is a level made from an order, not from the episodes
    _lvl(db_path, "z-lvl004", "s-lvl001", "tvdb_season", number=4, anilist=444)
    found = _by_rule(db_path)["R1.10a"]
    assert found.count == 1 and "S4" in found.samples[0]
