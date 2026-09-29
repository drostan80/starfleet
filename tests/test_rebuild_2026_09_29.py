"""The 2026-09-29 build: film shows carry `tvdb_movie` (R3.2a), one cour twice is one part
(R1.11), minis sit inside their group (R1.13c), and an unaired episode is never watched (R2.7)."""

import json

import pytest
from tests.test_rebuild_cleanup import NOW, _count, _episode, _event, _migrated, _season, _show

from lcars import numbering, rebuild, rebuild_cleanup, rulecheck


@pytest.fixture
def run(tmp_path):
    (tmp_path / "inputs" / "rebuild-inputs").mkdir(parents=True)
    r = rebuild.Run(tmp_path / "run", tmp_path / "snap.db", tmp_path / "live.db",
                    tmp_path / "inputs", stage="structure")
    r.dir.mkdir()
    return r


@pytest.fixture
def conn(run):
    return _migrated(run.work())


def _link(conn, sid, service, value):
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES (?, ?, ?, '', ?)", (sid, service, str(value), NOW))


def _span(conn, zid, a, b):
    conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                 (zid, a, b))


class TestFilmShows:
    def test_a_film_gets_its_tvdb_movie_id_and_loses_the_series_id_and_the_strays(
            self, run, conn):
        _show(conn, "s-film01", "Donnie Darko", shape="movie", tvdb="77623")
        _link(conn, "s-film01", "tvmaze", 553)
        _show(conn, "s-film02", "Sith", shape="movie", tvdb="75033")
        _show(conn, "s-show01", "A Series", tvdb="5")
        _episode(conn, "e-aaaaaa", "s-film02", 1, 1, "watched")
        _event(conn, "w-aaaaaa", "s-film02", 1, 1)
        (run.inputs / "rebuild-inputs" / "film_tvdb.json").write_text(json.dumps(
            {"s-film01": {"tvdb_movie": 658}, "s-film02": {"tvdb_movie": 875}}))

        rebuild_cleanup.film_ids(run, conn)

        links = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT show_id, service, external_id FROM show_external_id")}
        assert links[("s-film01", "tvdb_movie")] == "658"
        assert ("s-film01", "tvdb") not in links and ("s-film01", "tvmaze") not in links
        assert links[("s-show01", "tvdb")] == "5"  # a series is left alone
        assert _count(conn, "episode", "show_id = 's-film02'") == 0
        assert _count(conn, "watch_event", "show_id = 's-film02'") == 0
        assert (run.dir / "removed" / "episode.jsonl").exists()

    def test_a_film_that_lost_a_wrong_id_and_has_no_answer_is_left_for_review(self, run, conn):
        _show(conn, "s-film01", "Memories", shape="movie", tvdb="294395")
        _show(conn, "s-film02", "Never had one", shape="movie")  # nothing lost: not asked
        rebuild_cleanup.film_ids(run, conn)
        assert (run.dir / "ledger.jsonl").read_text().count('"review"') == 1


class TestPartDuplicates:
    def _cour(self, conn, sid, unmatched_status, entry_status):
        _show(conn, sid, "One Cour", tvdb="9")
        _season(conn, "z-shell1", sid, 1, status="watching")
        _season(conn, "z-loose1", sid, 1, status=unmatched_status, kind="part", parent="z-shell1")
        _season(conn, "z-entry1", sid, 1, anilist=210234, status=entry_status, kind="part",
                parent="z-shell1")
        for i in range(1, 4):
            _episode(conn, f"e-00000{i}", sid, 1, i)
            conn.execute("UPDATE episode SET season_id = 'z-loose1', absolute_number = ?"
                         " WHERE id = ?", (float(i), f"e-00000{i}"))

    def test_the_unmatched_part_and_the_entry_become_one_part_with_the_episodes_span(
            self, run, conn):
        self._cour(conn, "s-aaaaaa", "watching", "completed")
        rebuild_cleanup.merge_part_duplicates(run, conn, rebuild_cleanup.Deleter(conn, None))
        parts = conn.execute("SELECT id, anilist_id FROM season WHERE kind = 'part'").fetchall()
        assert [tuple(p) for p in parts] == [("z-entry1", 210234)]
        assert [tuple(r) for r in conn.execute(
            "SELECT abs_from, abs_to FROM season_span WHERE season_id = 'z-entry1'")] == [(1, 3)]
        assert _count(conn, "episode", "season_id = 'z-shell1'") == 3

    def test_two_parts_holding_the_same_entry_are_one(self, run, conn):
        _show(conn, "s-aaaaaa", "Twice", tvdb="9")
        _season(conn, "z-shell1", "s-aaaaaa", 1)
        _season(conn, "z-partaa", "s-aaaaaa", 1, anilist=7, kind="part", parent="z-shell1")
        _season(conn, "z-partbb", "s-aaaaaa", 1, anilist=7, kind="part", parent="z-shell1")
        conn.execute("UPDATE season SET part_number = 2 WHERE id = 'z-partbb'")
        rebuild_cleanup.merge_part_duplicates(run, conn, rebuild_cleanup.Deleter(conn, None))
        assert _count(conn, "season", "kind = 'part'") == 1


class TestMinisGroups:
    def test_a_mini_inside_the_groups_run_becomes_its_child_and_no_longer_overlaps(self, conn):
        _show(conn, "s-aaaaaa", "Minis", tvdb="3")
        _season(conn, "z-shell1", "s-aaaaaa", 1)
        for zid, label, spans in (("z-group1", "Season 1 minis", [(4.1, 4.1), (4.3, 4.3)]),
                                  ("z-mini01", "S00E10 A Podcast", [(4.2, 4.2)]),
                                  ("z-other1", "S00E11 Elsewhere", [(9.5, 9.5)])):
            conn.execute("INSERT INTO season (id, show_id, kind, parent_id, label, source,"
                         " status, created_at, updated_at) VALUES (?, 's-aaaaaa', 'special',"
                         " 'z-shell1', ?, 'auto', 'planned', ?, ?)", (zid, label, NOW, NOW))
            for a, b in spans:
                _span(conn, zid, a, b)
        assert numbering._nest_in_minis_groups(conn, "s-aaaaaa", NOW) == 1
        parents = dict(conn.execute("SELECT id, parent_id FROM season WHERE kind = 'special'"))
        assert parents == {"z-group1": "z-shell1", "z-mini01": "z-group1", "z-other1": "z-shell1"}


class TestRulecheck:
    def test_a_film_inside_another_films_span_is_not_an_overlap(self, conn):
        _show(conn, "s-aaaaaa", "Films", tvdb="4")
        for zid, a, b in (("z-filmaa", 52, 55), ("z-filmbb", 53, 53)):
            conn.execute("INSERT INTO season (id, show_id, kind, label, source, status,"
                         " created_at, updated_at) VALUES (?, 's-aaaaaa', 'special', 'film',"
                         " 'auto', 'planned', ?, ?)", (zid, NOW, NOW))
            _span(conn, zid, a, b)
        conn.commit()
        assert rulecheck.check_spans_do_not_overlap(conn).count == 0

    def test_a_level_with_episodes_and_no_span_is_a_violation_a_list_only_one_is_not(self, conn):
        _show(conn, "s-aaaaaa", "Spans", tvdb="4")
        _season(conn, "z-hasepi", "s-aaaaaa", 1)
        _season(conn, "z-liston", "s-aaaaaa", 2, anilist=5)
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 1, 1)
        conn.execute("UPDATE episode SET season_id = 'z-hasepi' WHERE id = 'e-aaaaaa'")
        conn.commit()
        assert rulecheck.check_seasons_have_spans(conn).count == 1


class TestReplayFindsTheEpisodeByIdentity:
    def test_a_live_row_on_other_coordinates_maps_to_its_own_episode(self, run, conn, tmp_path):
        import sqlite3

        from lcars import rebuild_replay

        _show(conn, "s-aaaaaa", "Shift", tvdb="1")
        _episode(conn, "e-aaaaaa", "s-aaaaaa", 2, 13)
        conn.execute("UPDATE episode SET title = 'The Visitors', air_date_utc ="
                     " '2021-07-06T00:00:00Z' WHERE id = 'e-aaaaaa'")
        _episode(conn, "e-bbbbbb", "s-aaaaaa", 3, 1)
        conn.execute("UPDATE episode SET title = 'Demons and Strategies', air_date_utc ="
                     " '2024-04-05T00:00:00Z' WHERE id = 'e-bbbbbb'")
        conn.commit()
        live = _migrated(tmp_path / "live.db")
        _show(live, "s-aaaaaa", "Shift", tvdb="1")
        _episode(live, "e-live01", "s-aaaaaa", 3, 1)  # live still numbers it the old way
        live.execute("UPDATE episode SET title = 'The Visitors', air_date_utc ="
                     " '2021-07-06T00:00:00Z', sonarr_season = 3, sonarr_episode = 1"
                     " WHERE id = 'e-live01'")
        live.commit()
        live.row_factory = sqlite3.Row
        mapper = rebuild_replay.Mapper(run, conn, live)
        row = mapper.episode("s-aaaaaa", 3, 1, "s-aaaaaa")
        assert (row["season"], row["episode"]) == (2, 13)
