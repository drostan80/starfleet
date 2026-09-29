"""The cleanup decisions of 2026-09-29 (rebuild_cleanup): FK-safe delete with export,
folds, statuses, the film's season, history mapping, the stub rules, and that an id
that no longer resolves stops the run."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import rebuild, rebuild_cleanup

NOW = "2026-01-01T00:00:00Z"
ROOT = Path(__file__).resolve().parent.parent


def _migrated(path: Path) -> sqlite3.Connection:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                   env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
                   check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture
def run(tmp_path):
    (tmp_path / "inputs").mkdir()
    r = rebuild.Run(tmp_path / "run", tmp_path / "snap.db", tmp_path / "live.db",
                    tmp_path / "inputs", stage="structure")
    r.dir.mkdir()
    return r


@pytest.fixture
def conn(run):
    return _migrated(run.work())


def _show(conn, sid, title, tracked=1, status="planned", shape="episodic", tvdb=None):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, ?, 'anime', ?, 'english', ?, ?, ?, ?)",
        (sid, shape, title, status, tracked, NOW, NOW))
    if tvdb:
        conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url,"
                     " created_at) VALUES (?, 'tvdb', ?, '', ?)", (sid, tvdb, NOW))


def _season(conn, zid, sid, n=1, anilist=None, mal=None, status="planned", kind="tvdb_season",
            parent=None):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id, mal_id, source, status, kind,"
        " parent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?)",
        (zid, sid, n if kind == "tvdb_season" else None, anilist, mal, status, kind, parent,
         NOW, NOW))
    for service, ext in (("anilist", anilist), ("mal", mal)):
        if ext:
            conn.execute("INSERT INTO season_external_id (season_id, service, external_id,"
                         " created_at) VALUES (?, ?, ?, ?)", (zid, service, str(ext), NOW))


def _episode(conn, eid, sid, s, e, state="unwatched"):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, ?, ?)", (eid, sid, s, e, state, NOW, NOW))


def _event(conn, wid, sid, s, e, at=NOW):
    conn.execute("INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (wid, sid, s, e, at, NOW))


def _count(conn, table, where="1=1", params=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


class TestDeleter:
    def test_deletes_a_show_with_everything_on_it_and_exports_it(self, conn, tmp_path):
        _show(conn, "s-stub01", "Stub")
        _show(conn, "s-keep01", "Keep")
        _season(conn, "z-stub01", "s-stub01", anilist=11)
        _season(conn, "z-part01", "s-stub01", n=2, kind="part", parent="z-stub01")
        conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to)"
                     " VALUES ('z-stub01', 1, 2)")
        _episode(conn, "e-stub01", "s-stub01", 1, 1, "watched")
        _event(conn, "w-stub01", "s-stub01", 1, 1)
        conn.execute("INSERT INTO show_merge (id, winner_show_id, loser_show_id, matched_on,"
                     " manifest, merged_at) VALUES ('y-merge1', 's-keep01', 's-stub01',"
                     " 'x', '{}', ?)", (NOW,))
        _episode(conn, "e-keep01", "s-keep01", 1, 1)
        conn.commit()
        d = rebuild_cleanup.Deleter(conn, tmp_path / "removed")
        d.show("s-stub01")
        conn.commit()
        assert _count(conn, "show", "id = 's-stub01'") == 0
        for table in ("season", "season_span", "season_external_id", "watch_event", "show_merge"):
            assert _count(conn, table) == 0, table
        assert _count(conn, "show", "id = 's-keep01'") == 1 and _count(conn, "episode") == 1
        shows = (tmp_path / "removed" / "show.jsonl").read_text().splitlines()
        assert [json.loads(x)["id"] for x in shows] == ["s-stub01"]
        assert (tmp_path / "removed" / "watch_event.jsonl").exists()
        assert d.counts["season"] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _inputs(**kw):
    base = {"unlink": [], "duplicate_shows": [], "movie_seasons": [], "history_moves": [],
            "delete_shows": [], "folds": [], "season_status": [], "show_status": [],
            "skip_list": []}
    base.update(kw)
    return base


class TestStructure:
    def test_a_stub_folds_into_its_parent_by_tvdb_id_with_your_status(self, conn, run):
        _show(conn, "s-paren1", "Parent", tvdb="500")
        _season(conn, "z-paren1", "s-paren1", anilist=1)
        _show(conn, "s-stub01", "Parent OVA", tracked=0)
        _season(conn, "z-stub01", "s-stub01", anilist=2)
        conn.commit()
        rebuild_cleanup.structure_actions(run, conn, _inputs(folds=[
            {"loser": "s-stub01", "parent": {"tvdb": "500"}, "status": "completed", "why": "t"}]))
        row = conn.execute("SELECT show_id, kind, status, status_set_manually FROM season"
                           " WHERE id = 'z-stub01'").fetchone()
        assert tuple(row) == ("s-paren1", "special", "completed", 1)
        redirects = json.loads((run.dir / "redirects.json").read_text())
        assert redirects == {"s-stub01": "s-paren1"}

    def test_a_parent_that_is_not_there_stops_the_run(self, conn, run):
        _show(conn, "s-stub01", "Orphan", tracked=0)
        conn.commit()
        with pytest.raises(rebuild.RebuildError):
            rebuild_cleanup.structure_actions(run, conn, _inputs(folds=[
                {"loser": "s-stub01", "parent": {"tvdb": "999"}, "why": "t"}]))

    def test_an_unknown_show_stops_the_run(self, conn, run):
        with pytest.raises(rebuild.RebuildError):
            rebuild_cleanup.structure_actions(run, conn, _inputs(delete_shows=[
                {"show": "s-nope01", "why": "t"}]))

    def test_a_film_with_no_season_gets_one_holding_both_list_ids(self, conn, run):
        _show(conn, "s-film01", "Witch", shape="movie")
        conn.commit()
        rebuild_cleanup.structure_actions(run, conn, _inputs(movie_seasons=[
            {"show": "s-film01", "anilist": 143103, "mal": 50668}]))
        z = conn.execute("SELECT * FROM season WHERE show_id = 's-film01'").fetchone()
        assert (z["anilist_id"], z["mal_id"], z["kind"], z["season_number"]) == (
            143103, 50668, "tvdb_season", 1)
        assert _count(conn, "season_external_id", "season_id = ?", (z["id"],)) == 2

    def test_a_duplicate_show_goes_when_another_shows_season_holds_its_id(self, conn, run):
        _show(conn, "s-main01", "Main")
        _season(conn, "z-main02", "s-main01", n=2, anilist=77)
        _show(conn, "s-dup001", "Main Season 2")
        _season(conn, "z-dup001", "s-dup001", anilist=77)
        _episode(conn, "e-dup001", "s-dup001", 1, 1)
        conn.commit()
        rebuild_cleanup.structure_actions(run, conn, _inputs(duplicate_shows=[
            {"show": "s-dup001", "anilist_held_by": 77}]))
        assert _count(conn, "show", "id = 's-dup001'") == 0
        assert _count(conn, "season", "id = 'z-main02'") == 1  # the season inside the show wins
        redirects = json.loads((run.dir / "redirects.json").read_text())
        assert redirects["s-dup001"] == "s-main01"

    def test_a_duplicate_with_watch_history_is_not_deleted(self, conn, run):
        _show(conn, "s-main01", "Main")
        _season(conn, "z-main02", "s-main01", n=2, anilist=77)
        _show(conn, "s-dup001", "Dup")
        _episode(conn, "e-dup001", "s-dup001", 1, 1, "watched")
        _event(conn, "w-dup001", "s-dup001", 1, 1)
        conn.commit()
        with pytest.raises(rebuild.RebuildError):
            rebuild_cleanup.structure_actions(run, conn, _inputs(duplicate_shows=[
                {"show": "s-dup001", "anilist_held_by": 77}]))

    def test_statuses_and_the_skip_list_are_yours(self, conn, run):
        _show(conn, "s-show01", "Show", tvdb="700")
        _season(conn, "z-show01", "s-show01", anilist=5)
        _season(conn, "z-show02", "s-show01", n=2, anilist=6)
        _show(conn, "s-skip01", "Skip Me", tracked=0)
        _season(conn, "z-skip01", "s-skip01", anilist=8)
        conn.commit()
        rebuild_cleanup.structure_actions(run, conn, _inputs(
            season_status=[{"service": "anilist", "id": 6, "status": "dropped", "why": "t"}],
            skip_list=["s-skip01"]))
        got = conn.execute("SELECT status, status_set_manually FROM season WHERE id ="
                           " 'z-show02'").fetchone()
        assert tuple(got) == ("dropped", 1)
        assert tuple(conn.execute("SELECT tracked, status FROM show WHERE id = 's-skip01'"
                                  ).fetchone()) == (0, "skipped")
        assert conn.execute("SELECT status FROM season WHERE id = 'z-skip01'"
                            ).fetchone()[0] == "skipped"

    def test_history_is_mapped_onto_the_right_show_episode_by_episode(self, conn, run):
        _show(conn, "s-good01", "Kaiju Girl", tvdb="471878")
        for n in (1, 2, 3):
            _episode(conn, f"e-good0{n}", "s-good01", 1, n)
        _event(conn, "w-good01", "s-good01", 1, 1)
        conn.execute("UPDATE episode SET state = 'watched' WHERE id = 'e-good01'")
        _show(conn, "s-stub01", "Kaiju Girl", tracked=0, status="watching")
        for n in (1, 2):
            _episode(conn, f"e-stub0{n}", "s-stub01", 1, n, "watched")
            _event(conn, f"w-stub0{n}", "s-stub01", 1, n, "2026-09-10T10:00:00Z")
        conn.commit()
        rebuild_cleanup.structure_actions(run, conn, _inputs(history_moves=[
            {"stub": "s-stub01", "into_tvdb": "471878"}]))
        states = dict(conn.execute("SELECT episode, state FROM episode WHERE show_id ="
                                   " 's-good01'"))
        assert states == {1: "watched", 2: "watched", 3: "unwatched"}
        assert _count(conn, "watch_event", "show_id = 's-good01'") == 2  # E1 was there
        assert _count(conn, "show", "id = 's-stub01'") == 0

    def test_history_that_cannot_be_placed_stops_the_run(self, conn, run):
        _show(conn, "s-good01", "Kaiju Girl", tvdb="471878")
        _episode(conn, "e-good01", "s-good01", 1, 1)
        _show(conn, "s-stub01", "Kaiju Girl", tracked=0)
        _episode(conn, "e-stub09", "s-stub01", 1, 9, "watched")
        _event(conn, "w-stub09", "s-stub01", 1, 9)
        conn.commit()
        with pytest.raises(rebuild.RebuildError):
            rebuild_cleanup.structure_actions(run, conn, _inputs(history_moves=[
                {"stub": "s-stub01", "into_tvdb": "471878"}]))


class TestStageCleanup:
    def test_stubs_go_but_a_list_entry_no_season_holds_and_history_stay(self, run, conn):
        _show(conn, "s-keep01", "Kept")
        _season(conn, "z-keep01", "s-keep01", anilist=1)
        _show(conn, "s-dupe01", "Duplicate of a kept season", tracked=0)
        _season(conn, "z-dupe01", "s-dupe01", anilist=1)
        _show(conn, "s-disc01", "Discovered", tracked=0)
        _season(conn, "z-disc01", "s-disc01", anilist=2)
        _show(conn, "s-list01", "On your list", tracked=0)
        _season(conn, "z-list01", "s-list01", anilist=3)
        _show(conn, "s-skip01", "Skip list", tracked=0, status="skipped")
        _show(conn, "s-hist01", "Has history", tracked=0)
        _episode(conn, "e-hist01", "s-hist01", 1, 1, "watched")
        conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                     " VALUES ('s-keep01', 'anilist', '1', '', ?)", (NOW,))
        conn.commit()
        conn.close()
        (run.inputs / "anilist_list.json").write_text(json.dumps(
            {"data": {"MediaListCollection": {"lists": [{"entries": [{"mediaId": 3}]}]}}}))
        run.stage = "cleanup"
        rebuild_cleanup.stage_cleanup(run)
        c = sqlite3.connect(run.work())
        left = {r[0] for r in c.execute("SELECT id FROM show")}
        assert left == {"s-keep01", "s-list01", "s-skip01", "s-hist01"}
        assert c.execute("SELECT COUNT(*) FROM show_external_id WHERE show_id = 's-keep01'"
                         ).fetchone()[0] == 0  # the redundant show-level id
        assert c.execute("SELECT COUNT(*) FROM season WHERE id = 'z-keep01'").fetchone()[0] == 1
        redirects = json.loads((run.dir / "redirects.json").read_text())
        assert set(redirects) == {"s-dupe01", "s-disc01"}
        ledger = [json.loads(x) for x in (run.dir / "ledger.jsonl").read_text().splitlines()]
        assert {e["key"] for e in ledger if e["outcome"] == "review"} == {
            "s-list01", "s-hist01"}
        assert (run.dir / "removed" / "show.jsonl").exists()

    def test_orphaned_watch_events_are_removed(self, run, conn):
        _show(conn, "s-keep01", "Kept")
        _episode(conn, "e-keep01", "s-keep01", 1, 1, "watched")
        _event(conn, "w-keep01", "s-keep01", 1, 1)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")  # the orphan exists in the real data
        _event(conn, "w-orph01", "s-keep01", 1, 54)
        conn.commit()
        conn.close()
        (run.inputs / "anilist_list.json").write_text(json.dumps(
            {"data": {"MediaListCollection": {"lists": []}}}))
        run.stage = "cleanup"
        rebuild_cleanup.stage_cleanup(run)
        c = sqlite3.connect(run.work())
        assert [r[0] for r in c.execute("SELECT id FROM watch_event")] == ["w-keep01"]

    def test_running_it_twice_changes_nothing_more(self, run, conn):
        _show(conn, "s-disc01", "Discovered", tracked=0)
        conn.commit()
        conn.close()
        (run.inputs / "anilist_list.json").write_text(json.dumps(
            {"data": {"MediaListCollection": {"lists": []}}}))
        run.stage = "cleanup"
        rebuild_cleanup.stage_cleanup(run)
        rebuild_cleanup.stage_cleanup(run)
        assert sqlite3.connect(run.work()).execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0
