"""Stage 8 (rebuild_replay): live ids mapped onto the rebuilt copy, a replayed watch's
effects through the status engine (R2.15a included), the statuses you gave, your manual
watches, and the Kaiju minis rule."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import config, rebuild, rebuild_replay

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


@pytest.fixture(autouse=True)
def _config():
    config.set_current(config.Config())
    yield
    config.set_current(config.Config())


@pytest.fixture
def run(tmp_path):
    (tmp_path / "inputs").mkdir()
    r = rebuild.Run(tmp_path / "run", tmp_path / "snap.db", tmp_path / "live.db",
                    tmp_path / "inputs", stage="replay")
    r.dir.mkdir()
    return r


@pytest.fixture
def conn(run):
    return _migrated(run.work())


@pytest.fixture
def live(run):
    return _migrated(run.live)


def _show(c, sid, title, tracked=1, status="planned", tvdb=None):
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'english', ?, ?, ?, ?)",
        (sid, title, status, tracked, NOW, NOW))
    if tvdb:
        c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                  " VALUES (?, 'tvdb', ?, '', ?)", (sid, tvdb, NOW))


def _season(c, zid, sid, n, status="planned", total=None, anilist=None):
    c.execute(
        "INSERT INTO season (id, show_id, season_number, source, status, kind, episode_total,"
        " anilist_id, created_at, updated_at) VALUES (?, ?, ?, 'manual', ?, 'tvdb_season', ?, ?,"
        " ?, ?)", (zid, sid, n, status, total, anilist, NOW, NOW))


def _episode(c, eid, sid, s, e, air="2026-09-01T00:00:00Z", title=None, sonarr=None, absolute=None):
    c.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, title,"
        " sonarr_season, sonarr_episode, absolute_number, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, ?, ?, ?, ?, ?, ?)",
        (eid, sid, s, e, air, title, sonarr[0] if sonarr else None, sonarr[1] if sonarr else None,
         absolute, NOW, NOW))


def _ledger(run):
    path = run.dir / "ledger.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class TestMapper:
    def test_same_id_redirect_and_ids_of_a_show_created_after_the_base(self, run, conn, live):
        _show(conn, "s-same01", "Same")
        _show(conn, "s-win001", "Winner")
        _show(conn, "s-newrun", "New", tvdb="777")
        (run.dir / "redirects.json").write_text(
            json.dumps({"s-lose01": "s-win001", "s-gone01": None}))
        _show(live, "s-newliv", "New", tvdb="777")
        live.commit()
        m = rebuild_replay.Mapper(run, conn, live)
        assert m.show("s-same01") == "s-same01"
        assert m.show("s-lose01") == "s-win001"
        assert m.show("s-gone01") is None
        assert m.show("s-newliv") == "s-newrun"   # by its TVDB id
        assert m.show("s-nope01") is None

    def test_an_episode_is_found_by_sonarr_coordinates_then_absolute_number_and_date(
            self, run, conn, live):
        _show(conn, "s-show01", "Show")
        _episode(conn, "e-a00001", "s-show01", 1, 57, absolute=57.0, air="2026-09-20T00:00:00Z")
        _episode(conn, "e-b00001", "s-show01", 1, 3)
        _show(live, "s-show01", "Show")
        _episode(live, "e-live01", "s-show01", 4, 21, sonarr=(1, 3))          # coordinates match
        _episode(live, "e-live02", "s-show01", 4, 22, absolute=57.0, air="2026-09-20T00:00:00Z")
        live.commit()
        m = rebuild_replay.Mapper(run, conn, live)
        assert m.episode("s-show01", 4, 21, "s-show01")["id"] == "e-b00001"
        assert m.episode("s-show01", 4, 22, "s-show01")["id"] == "e-a00001"
        assert m.episode("s-show01", 9, 9, "s-show01") is None


class TestReplayWatch:
    def test_a_watch_marks_the_episode_and_moves_the_season_to_watching(self, conn):
        _show(conn, "s-show01", "Show")
        _season(conn, "z-show01", "s-show01", 1, "planned")
        _episode(conn, "e-show01", "s-show01", 1, 1)
        _episode(conn, "e-show02", "s-show01", 1, 2)
        conn.commit()
        ep = conn.execute("SELECT * FROM episode WHERE id = 'e-show01'").fetchone()
        rebuild_replay.replay_watch(conn, "s-show01", ep, "2026-09-10T10:00:00Z", "plex", NOW)
        assert conn.execute("SELECT state FROM episode WHERE id = 'e-show01'"
                            ).fetchone()[0] == "watched"
        assert conn.execute("SELECT status FROM season WHERE id = 'z-show01'"
                            ).fetchone()[0] == "watching"
        w = conn.execute("SELECT watched_at, platform FROM watch_event").fetchone()
        assert tuple(w) == ("2026-09-10T10:00:00Z", "plex")

    def test_overgeared_one_known_episode_is_watching_not_completed(self, conn):
        _show(conn, "s-over01", "Overgeared")
        _season(conn, "z-over01", "s-over01", 1, "planned", anilist=212888)  # no total yet
        _episode(conn, "e-over01", "s-over01", 1, 1)
        conn.commit()
        ep = conn.execute("SELECT * FROM episode WHERE id = 'e-over01'").fetchone()
        rebuild_replay.replay_watch(conn, "s-over01", ep, "2026-09-27T21:00:00Z", None, NOW)
        assert conn.execute("SELECT status FROM season WHERE id = 'z-over01'"
                            ).fetchone()[0] == "watching"


def _decisions(**kw):
    base = {"gap_watch": [], "gap_status": [], "manual_watches": []}
    base.update(kw)
    return base


class TestStatuses:
    def _mapper(self, run, conn, live):
        return rebuild_replay.Mapper(run, conn, live)

    def test_finals_are_applied_and_discards_are_not(self, run, conn, live):
        _show(conn, "s-watch1", "Watching Show")
        _season(conn, "z-watch1", "s-watch1", 1, "planned")
        _show(conn, "s-drop01", "Dropped Show", status="watching")
        _season(conn, "z-drop01", "s-drop01", 1, "watching")
        _show(conn, "s-disc01", "Discarded")
        _season(conn, "z-disc01", "s-disc01", 1, "planned")
        conn.commit()
        d = _decisions(gap_status=[
            {"key": "a", "show": "s-watch1", "title": "W", "final": "watching", "change": "x"},
            {"key": "b", "show": "s-drop01", "title": "D", "final": "dropped", "change": "x"},
            {"key": "c", "show": "s-disc01", "title": "X", "final": "discard", "change": "x"},
            {"key": "d", "show": "s-drop01", "title": "D", "final": "replay",
             "change": "watching → paused"}])
        touched: set = set()
        rebuild_replay.replay_statuses(run, conn, d, self._mapper(run, conn, live), touched)
        status = dict(conn.execute("SELECT id, status FROM season"))
        assert status["z-watch1"] == "watching"
        assert status["z-drop01"] == "paused"      # the last replayed change wins
        assert status["z-disc01"] == "planned"
        assert touched == {"s-watch1", "s-drop01"}

    def test_a_film_with_no_tvdb_season_takes_completed_as_it_is(self, run, conn, live):
        _show(conn, "s-film01", "A Film", status="planned")
        conn.commit()
        d = _decisions(gap_status=[{"key": "a", "show": "s-film01", "title": "A Film",
                                    "final": "completed", "change": "x"}])
        rebuild_replay.replay_statuses(run, conn, d, self._mapper(run, conn, live), set())
        assert conn.execute("SELECT status FROM show WHERE id = 's-film01'"
                            ).fetchone()[0] == "completed"

    def test_a_status_for_a_show_that_is_not_there_is_a_review_line(self, run, conn, live):
        d = _decisions(gap_status=[{"key": "a", "show": "s-nope01", "title": "Nope",
                                    "final": "completed", "change": "x"}])
        rebuild_replay.replay_statuses(run, conn, d, self._mapper(run, conn, live), set())
        assert [e["outcome"] for e in _ledger(run)] == ["review"]


class TestManualAndKaiju:
    def test_a_manual_watch_finds_the_show_by_a_unique_title_prefix(self, run, conn):
        _show(conn, "s-mush01", "Mushoku Tensei: Jobless Reincarnation")
        _season(conn, "z-mush01", "s-mush01", 3, "planned")
        _episode(conn, "e-mush01", "s-mush01", 3, 1, air="2026-07-01T00:00:00Z")
        _episode(conn, "e-mush02", "s-mush01", 3, 2, air="2026-08-01T00:00:00Z")
        conn.commit()
        d = _decisions(manual_watches=[{"key": "mw:mushoku-s3-final", "title": "Mushoku Tensei",
                                        "season": 3, "episode": "last aired",
                                        "at": "2026-09-27T20:15:00Z"}])
        rebuild_replay.replay_manual(run, conn, d, set())
        assert conn.execute("SELECT state FROM episode WHERE id = 'e-mush02'"
                            ).fetchone()[0] == "watched"          # the last aired one
        assert conn.execute("SELECT status FROM season WHERE id = 'z-mush01'"
                            ).fetchone()[0] == "completed"          # "S3 finished → completed"

    def test_the_minis_are_replayed_only_on_an_exact_title_and_air_date(self, run, conn, live):
        _show(conn, "s-kaiju1", "Kaiju No. 8")
        _show(live, "s-kaiju1", "Kaiju No. 8")
        air = "2026-09-12T00:00:00Z"
        _episode(conn, "e-good01", "s-kaiju1", 0, 5, air=air, title="Mini #2")
        _episode(conn, "e-other1", "s-kaiju1", 0, 6, air="2026-09-19T00:00:00Z", title="Mini #3")
        _episode(live, "e-live01", "s-kaiju1", 0, 5, air=air, title="Mini #2")
        _episode(live, "e-live02", "s-kaiju1", 0, 6, air="2026-09-19T00:00:00Z", title="Other")
        for i, n in ((1, 5), (2, 6)):
            live.execute("INSERT INTO watch_event (id, show_id, season, episode, watched_at,"
                         " created_at) VALUES (?, 's-kaiju1', 0, ?, '2026-09-13T10:00:00Z', ?)",
                         (f"w-live00{i}", n, NOW))
        live.commit()
        conn.commit()
        g = {"key": "gw", "show": "s-kaiju1", "replay": "realign", "title": "Kaiju",
             "episodes": "S0E5, S0E6"}
        touched: set = set()
        rebuild_replay._kaiju_minis(run, conn, live, g, rebuild_replay.Mapper(run, conn, live),
                                    touched)
        assert conn.execute("SELECT state FROM episode WHERE id = 'e-good01'"
                            ).fetchone()[0] == "watched"
        assert conn.execute("SELECT state FROM episode WHERE id = 'e-other1'"
                            ).fetchone()[0] == "unwatched"        # title differs: review
        assert sorted(e["outcome"] for e in _ledger(run)) == ["applied", "review"]
