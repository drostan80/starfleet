"""Stage 4 pre-pass (rebuild_reanchor): rows that sit on another episode's TVDB coordinates
after TVDB renumbered a show go to their own episode, by title and air date."""

import sqlite3

import pytest

from lcars import rebuild_reanchor as ra
from tests.test_rebuild_cleanup import NOW, _count, _episode, _event, _migrated, _season, _show


def ep(s, e, title, date):
    return {"seasonNumber": s, "episodeNumber": e, "title": title, "airDate": date}


def row(rid, s, e, title, date, created="2026-01-01", watched=False):
    return {"id": rid, "season": s, "episode": e, "title": title, "date": date,
            "created_at": created, "watched": watched}


# ── the plan ─────────────────────────────────────────────────────────────


def test_a_row_on_its_own_coordinates_is_stable():
    p = ra.plan([row("a", 1, 1, "Pilot", "2020-01-01T00:00:00Z")], [ep(1, 1, "Pilot", "2020-01-01")])
    assert p["stable"] == ["a"] and not ra.needs_reanchor(p)


def test_a_renamed_episode_with_the_same_date_is_stable():
    p = ra.plan([row("a", 1, 1, "After Ten Years", "2026-07-26T00:00:00Z")],
                [ep(1, 1, "After 10 Years", "2026-07-27")])
    assert p["stable"] == ["a"]


def test_a_tba_title_is_never_a_difference():
    p = ra.plan([row("a", 1, 9, "TBA", "2026-08-19T00:00:00Z")],
                [ep(1, 9, "Hope Like Never Before", "2026-08-27")])
    assert p["stable"] == ["a"] and not ra.needs_reanchor(p)


def test_slime_style_shift_moves_each_row_to_its_own_episode():
    # the row at S3E1 is "The Visitors" (2021); TVDB's S3E1 is another episode, "The Visitors"
    # is TVDB's S2E13
    rows = [row("old", 3, 1, "The Visitors", "2021-07-06T00:00:00Z"),
            row("new", 4, 1, "Demons and Strategies", "2024-04-05T00:00:00Z")]
    eps = [ep(2, 13, "The Visitors", "2021-07-06"), ep(3, 1, "Demons and Strategies", "2024-04-05")]
    p = ra.plan(rows, eps)
    assert p["target"] == {"old": (2, 13), "new": (3, 1)}
    assert sorted(p["moved"]) == ["new", "old"]


def test_the_same_title_a_week_apart_is_the_same_episode():
    p = ra.plan([row("a", 3, 1, "The Brokenhearted Mage", "2023-07-02T00:00:00Z")],
                [ep(2, 1, "The Brokenhearted Mage", "2023-07-10")])
    assert p["target"]["a"] == (2, 1)


def test_two_rows_that_are_one_episode_leave_one():
    rows = [row("kept", 2, 1, "Pilot", "2020-01-01T00:00:00Z", created="2020-01-02", watched=True),
            row("copy", 3, 1, "Pilot", "2020-01-01T00:00:00Z", created="2026-09-01")]
    p = ra.plan(rows, [ep(2, 1, "Pilot", "2020-01-01")])
    assert p["duplicates"] == [{"target": (2, 1), "keep": "kept", "drop": ["copy"]}]


def test_untitled_rows_of_a_shifted_show_are_placed_by_date_an_airing_show_is_left_alone():
    shifted = [row("a", 4, 1, "Real", "2026-01-01T00:00:00Z"),
               row("b", 4, 2, "TBA", "2026-01-08T00:00:00Z")]
    eps = [ep(3, 1, "Real", "2026-01-01"), ep(3, 2, "Named Now", "2026-01-08")]
    assert ra.plan(shifted, eps)["target"]["b"] == (3, 2)
    airing = [row("b", 1, 2, "TBA", "2026-01-08T00:00:00Z")]
    assert not ra.needs_reanchor(ra.plan(airing, [ep(1, 1, "Named", "2026-01-08"),
                                                  ep(1, 2, "Later", "2026-01-15")]))


# ── applying it ──────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    return _migrated(tmp_path / "t.db")


@pytest.fixture(autouse=True)
def no_fribb(monkeypatch):
    monkeypatch.setitem(ra._FRIBB, "index", None)


def _titled(conn, eid, sid, s, e, title, date, state="unwatched"):
    _episode(conn, eid, sid, s, e, state)
    conn.execute("UPDATE episode SET title = ?, air_date_utc = ? WHERE id = ?",
                 (title, date, eid))


def test_two_cours_become_one_season_with_parts_and_the_history_follows(conn):
    _show(conn, "s-aaaaaa", "Cours", tvdb="1")
    _season(conn, "z-aaaaaa", "s-aaaaaa", 2, anilist=10, status="completed")
    _season(conn, "z-bbbbbb", "s-aaaaaa", 3, anilist=20, status="completed")
    _titled(conn, "e-aaaaaa", "s-aaaaaa", 2, 1, "One", "2021-01-01T00:00:00Z", "watched")
    _titled(conn, "e-bbbbbb", "s-aaaaaa", 3, 1, "Two", "2021-07-01T00:00:00Z", "watched")
    for eid, s, e in (("e-aaaaaa", 2, 1), ("e-bbbbbb", 3, 1)):
        conn.execute("UPDATE episode SET season_id = ? WHERE id = ?",
                     ("z-aaaaaa" if s == 2 else "z-bbbbbb", eid))
    _event(conn, "w-aaaaaa", "s-aaaaaa", 2, 1)
    _event(conn, "w-bbbbbb", "s-aaaaaa", 3, 1, "2026-02-02T00:00:00Z")
    eps = [ep(2, 1, "One", "2021-01-01"), ep(2, 2, "Two", "2021-07-01")]

    out = ra.reanchor_show(None, conn, "s-aaaaaa", eps)

    assert out["moved"] == 1
    moved = conn.execute("SELECT season, episode, sonarr_season, sonarr_episode, state,"
                         " season_id FROM episode WHERE id = 'e-bbbbbb'").fetchone()
    assert tuple(moved)[:5] == (2, 2, 2, 2, "watched")
    assert moved["season_id"] == "z-aaaaaa"
    ev = conn.execute("SELECT season, episode, watched_at FROM watch_event WHERE id = 'w-bbbbbb'"
                      ).fetchone()
    assert tuple(ev) == (2, 2, "2026-02-02T00:00:00Z")
    parts = conn.execute("SELECT anilist_id, part_number FROM season WHERE parent_id ="
                         " 'z-aaaaaa' AND kind = 'part' ORDER BY part_number").fetchall()
    assert [tuple(r) for r in parts] == [(10, 1), (20, 2)]
    assert conn.execute("SELECT anilist_id FROM season WHERE id = 'z-aaaaaa'").fetchone()[0] is None
    assert _count(conn, "season", "show_id = 's-aaaaaa' AND kind = 'tvdb_season'") == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_duplicate_row_goes_with_its_history_moved_to_the_kept_one(conn, tmp_path):
    _show(conn, "s-aaaaaa", "Dup", tvdb="1")
    _season(conn, "z-aaaaaa", "s-aaaaaa", 2, anilist=10)
    _season(conn, "z-bbbbbb", "s-aaaaaa", 3, anilist=20)
    _titled(conn, "e-aaaaaa", "s-aaaaaa", 2, 1, "One", "2021-01-01T00:00:00Z")
    _titled(conn, "e-bbbbbb", "s-aaaaaa", 3, 1, "One", "2021-01-01T00:00:00Z", "watched")
    conn.execute("UPDATE episode SET created_at = '2026-09-01T00:00:00Z' WHERE id = 'e-aaaaaa'")
    _event(conn, "w-bbbbbb", "s-aaaaaa", 3, 1, "2021-03-03T00:00:00Z")

    class Run:
        dir = tmp_path
        notes: list = []

        def record(self, *a):
            self.notes.append(a)

    out = ra.reanchor_show(Run(), conn, "s-aaaaaa", [ep(2, 1, "One", "2021-01-01")])

    assert out["duplicates_removed"] == 1
    assert _count(conn, "episode", "show_id = 's-aaaaaa'") == 1
    kept = conn.execute("SELECT id, state FROM episode WHERE show_id = 's-aaaaaa'").fetchone()
    assert (kept["id"], kept["state"]) == ("e-bbbbbb", "watched")  # the row with history stays
    assert conn.execute("SELECT season, episode FROM watch_event WHERE id = 'w-bbbbbb'"
                        ).fetchone()[:] == (2, 1)
    assert (tmp_path / "removed" / "episode.jsonl").exists()
    assert isinstance(conn, sqlite3.Connection)
