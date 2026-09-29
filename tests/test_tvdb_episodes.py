"""Episode lists read straight from TVDB (R1.2e, 2026-09-29): the client's
paginated read, the insert-only fill, the date fill, and the rebuild step."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lcars import airdate_priority, rebuild, tvdb_client, tvdb_episodes


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "tvdb_episodes_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _show(conn, show_id, tvdb_id=None, tracked=1, shape="episodic"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, ?, 'tv', 'T', 'romaji', 'planned', ?, 'x', 'x')", (show_id, shape, tracked))
    if tvdb_id:
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tvdb', ?, 'u', 'x')", (show_id, str(tvdb_id)))
    conn.commit()


def _client(pages: dict) -> tvdb_client.TvdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"data": {"token": "t"}})
        sid = int(request.url.path.split("/")[3])
        if sid not in pages:
            return httpx.Response(404)
        page = int(request.url.params.get("page", 0))
        eps = pages[sid][page]
        nxt = "next" if page + 1 < len(pages[sid]) else None
        return httpx.Response(200, json={"data": {"episodes": eps}, "links": {"next": nxt}})
    return tvdb_client.TvdbClient(
        "k", client=httpx.Client(base_url=tvdb_client.BASE_URL,
                                 transport=httpx.MockTransport(handler)))


def _ep(season, number, aired=None, absolute=None, name=None):
    return {"seasonNumber": season, "number": number, "aired": aired,
            "absoluteNumber": absolute, "name": name, "runtime": 24}


class TestClient:
    def test_paginates_and_normalises(self):
        c = _client({1: [[_ep(1, 1, "2020-01-05", 1, "Pilot")], [_ep(0, 1, "", None, "")]]})
        assert c.series_episodes(1) == [
            {"season": 1, "episode": 1, "aired": "2020-01-05", "title": "Pilot",
             "absolute": 1, "runtime": 24},
            {"season": 0, "episode": 1, "aired": None, "title": None,
             "absolute": None, "runtime": 24},
        ]

    def test_unknown_series_is_none(self):
        assert _client({}).series_episodes(9) is None

    def test_episode_without_numbers_is_skipped(self):
        c = _client({1: [[_ep(None, 1), _ep(1, None), _ep(1, 2)]]})
        assert [e["episode"] for e in c.series_episodes(1)] == [2]


class TestInsert:
    def test_inserts_only_missing_and_returns_dates(self, conn):
        _show(conn, "s-aaaaaa", 1)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, title, created_at,"
            " updated_at) VALUES ('e-000001', 's-aaaaaa', 1, 1, 'regular', 'mine', 'x', 'x')")
        eps = [{"season": 1, "episode": 1, "aired": "2020-01-05", "title": "theirs",
                "absolute": 1, "runtime": 24},
               {"season": 1, "episode": 2, "aired": "2020-01-12", "title": "Two",
                "absolute": 2, "runtime": 24},
               {"season": 0, "episode": 1, "aired": None, "title": "Special",
                "absolute": None, "runtime": None}]
        n, dates = tvdb_episodes.insert_missing(conn, "s-aaaaaa", eps)
        assert n == 2
        assert dates == {(1, 2): "2020-01-12"}
        rows = {(r["season"], r["episode"]): r for r in conn.execute("SELECT * FROM episode")}
        assert rows[(1, 1)]["title"] == "mine"  # never overwritten
        assert rows[(1, 2)]["kind"] == "regular" and rows[(1, 2)]["tvdb_absolute"] == 2
        assert rows[(1, 2)]["absolute_number"] is None  # Memory Alpha's (R1.2c)
        assert rows[(1, 2)]["air_date_utc"] is None
        assert rows[(0, 1)]["kind"] == "special"

    def test_attaches_to_an_existing_tvdb_season(self, conn):
        _show(conn, "s-aaaaaa", 1)
        conn.execute(
            "INSERT INTO season (id, show_id, season_number, status, source, matched,"
            " manual_override, created_at, updated_at) VALUES ('z-000001', 's-aaaaaa', 1,"
            " 'planned', 'unmatched', 0, 0, 'x', 'x')")
        tvdb_episodes.insert_missing(conn, "s-aaaaaa", [
            {"season": 1, "episode": 1, "aired": None, "title": None, "absolute": None,
             "runtime": None},
            {"season": 0, "episode": 1, "aired": None, "title": None, "absolute": None,
             "runtime": None}])
        by = {r["season"]: r["season_id"] for r in conn.execute("SELECT * FROM episode")}
        assert by == {1: "z-000001", 0: None}

    def test_date_fill_only_takes_empty_dates(self, conn):
        _show(conn, "s-aaaaaa", 1)
        for i, d in ((1, "2020-01-05T20:00:00Z"), (2, None)):
            conn.execute(
                "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc,"
                " air_date_source, created_at, updated_at) VALUES (?, 's-aaaaaa', 1, ?,"
                " 'regular', ?, ?, 'x', 'x')", (f"e-00000{i}", i, d, "tvmaze" if d else None))
        n = tvdb_episodes.fill_air_dates(
            conn, "s-aaaaaa", {(1, 1): "2020-01-05", (1, 2): "2020-01-12"})
        assert n == 1
        got = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT episode, air_date_utc, air_date_source FROM episode")}
        assert got == {1: ("2020-01-05T20:00:00Z", "tvmaze"), 2: ("2020-01-12T00:00:00Z", "tvdb")}


class TestPriority:
    def test_tvdb_is_a_weak_source(self):
        # A finer source may correct TVDB's date-only value in either direction.
        assert airdate_priority.should_apply(
            "anidb", "2020-01-13T00:00:00Z", "tvdb", "2020-01-12T00:00:00Z")


class TestRebuildStep:
    def _run(self, tmp_path):
        return rebuild.Run(tmp_path, tmp_path, tmp_path, tmp_path, stage="sonarr")

    def test_reads_only_shows_without_episodes(self, conn, tmp_path):
        _show(conn, "s-aaaaaa", 1)                       # no episodes: read
        _show(conn, "s-bbbbbb", 2)                       # has episodes: left alone
        _show(conn, "s-cccccc", 3)                       # not on TVDB: review
        _show(conn, "s-dddddd", 4, tracked=0)            # untracked: left alone
        _show(conn, "s-eeeeee", 5, shape="movie")        # movie: left alone
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, created_at, updated_at)"
            " VALUES ('e-000001', 's-bbbbbb', 1, 1, 'regular', 'x', 'x')")
        conn.commit()
        run = self._run(tmp_path)
        dates = rebuild._tvdb_episodes(
            run, conn, client=_client({1: [[_ep(1, 1, "2020-01-05", 1)]]}))
        assert dates == {"s-aaaaaa": {(1, 1): "2020-01-05"}}
        assert conn.execute("SELECT COUNT(*) FROM episode WHERE show_id = 's-aaaaaa'"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM episode WHERE show_id = 's-bbbbbb'"
                            ).fetchone()[0] == 1
        ledger = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
        assert [(e["key"], e["outcome"]) for e in ledger if e["outcome"] == "review"] == [
            ("s-cccccc", "review")]
        assert (tmp_path / "tvdb_episodes.json").exists()

    def test_cached_answers_are_not_read_again(self, conn, tmp_path):
        _show(conn, "s-aaaaaa", 1)
        run = self._run(tmp_path)
        rebuild._tvdb_episodes(run, conn, client=_client({1: [[_ep(1, 1)]]}))
        conn.execute("DELETE FROM episode")
        conn.commit()
        rebuild._tvdb_episodes(run, conn, client=_client({}))  # a read now would be a 404
        assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 1

    def test_a_tvdb_error_stops_the_run_after_the_ledger_line(self, conn, tmp_path):
        _show(conn, "s-aaaaaa", 1)
        def boom(request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"data": {"token": "t"}})
            return httpx.Response(500)
        client = tvdb_client.TvdbClient("k", client=httpx.Client(
            base_url=tvdb_client.BASE_URL, transport=httpx.MockTransport(boom)))
        with pytest.raises(rebuild.RebuildError):
            rebuild._tvdb_episodes(self._run(tmp_path), conn, client=client)
