"""LCARS → Jellyfin: watched episodes and movies are marked played for one user (2026-10-10)."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lcars import config, external_writes, jellyfin_client, jellyfin_sync

NOW = "2026-01-01T00:00:00Z"
UID = "u-media"


class FakeJellyfin:
    """The calls jellyfin_sync makes, over in-memory series, episodes and movies."""

    def __init__(self, series=(), episodes=None, movies=(), fail=None):
        self.series, self.episodes, self.movies = list(series), episodes or {}, list(movies)
        self.played, self.unplayed, self.fail = [], [], fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _check(self):
        if self.fail:
            raise self.fail

    def user_id(self, user):
        self._check()
        return UID

    def all_series(self, user_id):
        return self.series

    def all_movies(self, user_id):
        return self.movies

    def series_episodes(self, series_id, user_id):
        return self.episodes.get(series_id, [])

    def mark_played(self, item_id, user_id, date_played=None):
        self.played.append((item_id, date_played))
        for item in [i for items in self.episodes.values() for i in items] + self.movies:
            if item["Id"] == item_id:
                item["UserData"] = {"Played": True}

    def mark_unplayed(self, item_id, user_id):
        self.unplayed.append(item_id)
        for item in [i for items in self.episodes.values() for i in items] + self.movies:
            if item["Id"] == item_id:
                item["UserData"] = {"Played": False}


def jf_episode(item_id, season, number, played=False, end=None):
    item = {"Id": item_id, "ParentIndexNumber": season, "IndexNumber": number,
            "UserData": {"Played": played}}
    if end:
        item["IndexNumberEnd"] = end
    return item


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("jf") / "t.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    return path


@pytest.fixture
def conn(template, tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    shutil.copy(template, path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    config.set_current(config.Config(
        jellyfin_url="http://j", jellyfin_api_key="k", jellyfin_user="media"))
    monkeypatch.setattr(external_writes, "capturing", lambda: False)  # prod-like: writes are sent
    yield c
    config.set_current(config.Config())
    c.close()


def add_show(conn, show_id, tvdb, shape="episodic", status="watching"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " tracked, created_at, updated_at) VALUES (?, ?, 'tv', ?, 'romaji', ?, 1, ?, ?)",
        (show_id, shape, show_id, status, NOW, NOW))
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, '', ?)",
        (show_id, "tmdb" if shape == "movie" else "tvdb", str(tvdb), NOW))


def add_episode(conn, ep_id, show_id, season, number, state, watched_at=None):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at,"
        " sonarr_season, sonarr_episode) VALUES (?, ?, ?, ?, 'regular', ?, ?, ?, ?, ?)",
        (ep_id, show_id, season, number, state, NOW, NOW, season, number))
    if watched_at:
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (f"w-{ep_id[2:]}", show_id, season, number, watched_at, NOW))


def run(conn, monkeypatch, fake, **kw):
    monkeypatch.setattr(jellyfin_client, "JellyfinClient", lambda *a, **k: fake)
    return jellyfin_sync.run(conn, **kw)


def series(tvdb, item_id="J-S1"):
    return {"Id": item_id, "ProviderIds": {"Tvdb": str(tvdb)}}


def test_a_watched_episode_is_marked_played_with_the_time_you_watched_it(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    add_episode(conn, "e-aaaaa2", "s-aaaaaa", 1, 2, "unwatched")
    fake = FakeJellyfin(
        [series(42)], {"J-S1": [jf_episode("J-E1", 1, 1), jf_episode("J-E2", 1, 2)]})
    stats = run(conn, monkeypatch, fake)
    assert fake.played == [("J-E1", "2026-03-04T20:30:00Z")] and fake.unplayed == []
    assert stats["marked_played"] == 1 and stats["episodes_matched"] == 2
    row = conn.execute(
        "SELECT state FROM jellyfin_watch_sync WHERE entity_id = 'e-aaaaa1'").fetchone()
    assert row["state"] == "played"


def test_a_second_pass_sends_nothing(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1)]})
    run(conn, monkeypatch, fake)
    stats = run(conn, monkeypatch, fake)
    assert len(fake.played) == 1 and stats["marked_played"] == 0 and stats["agreed"] == 1


def test_un_watching_in_lcars_un_marks_what_was_agreed(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1)]})
    run(conn, monkeypatch, fake)
    conn.execute("UPDATE episode SET state = 'unwatched' WHERE id = 'e-aaaaa1'")
    stats = run(conn, monkeypatch, fake)
    assert fake.unplayed == ["J-E1"] and stats["marked_unplayed"] == 1
    assert run(conn, monkeypatch, fake)["marked_unplayed"] == 0  # and only once


def test_a_play_that_exists_only_in_jellyfin_is_never_undone(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1, played=True)]})
    stats = run(conn, monkeypatch, fake)
    assert fake.unplayed == [] and fake.played == [] and stats["marked_unplayed"] == 0


def test_one_file_for_two_episodes_is_played_only_when_both_are_watched(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    add_episode(conn, "e-aaaaa2", "s-aaaaaa", 1, 2, "unwatched")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E12", 1, 1, end=2)]})
    run(conn, monkeypatch, fake)
    assert fake.played == []
    conn.execute("UPDATE episode SET state = 'watched' WHERE id = 'e-aaaaa2'")
    run(conn, monkeypatch, fake)
    assert [p[0] for p in fake.played] == ["J-E12"]


def test_nothing_is_guessed_an_unmatched_episode_is_reported(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    add_episode(conn, "e-aaaaa5", "s-aaaaaa", 1, 5, "watched", "2026-03-05T20:30:00Z")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1)]})
    stats = run(conn, monkeypatch, fake)
    assert [p[0] for p in fake.played] == ["J-E1"]
    assert any("S01E05" in text for text in stats["unmatched"])


def test_a_show_jellyfin_does_not_have_costs_nothing(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    fake = FakeJellyfin([series(99)], {})
    stats = run(conn, monkeypatch, fake)
    assert stats["shows_checked"] == 0 and fake.played == []


def test_a_completed_movie_is_marked_played_and_a_planned_one_is_not(conn, monkeypatch):
    add_show(conn, "s-movie1", 777, shape="movie", status="completed")
    add_show(conn, "s-movie2", 778, shape="movie", status="planned")
    conn.execute("INSERT INTO episode (id, show_id, season, episode, kind, state, created_at,"
                 " updated_at) VALUES ('e-movie1', 's-movie1', 1, 1, 'regular', 'watched', ?, ?)",
                 (NOW, NOW))
    conn.execute("INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
                 " VALUES ('w-movie1', 's-movie1', 1, 1, '2026-02-02T21:00:00Z', ?)", (NOW,))
    movies = [{"Id": "J-M1", "ProviderIds": {"Tmdb": "777"}, "UserData": {"Played": False}},
              {"Id": "J-M2", "ProviderIds": {"Tmdb": "778"}, "UserData": {"Played": False}}]
    fake = FakeJellyfin(movies=movies)
    stats = run(conn, monkeypatch, fake)
    assert fake.played == [("J-M1", "2026-02-02T21:00:00Z")] and stats["movies_marked"] == 1


def test_a_dry_run_writes_nothing_anywhere(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1)]})
    stats = run(conn, monkeypatch, fake, dry_run=True)
    assert stats["marked_played"] == 1 and fake.played == []
    assert conn.execute("SELECT COUNT(*) FROM jellyfin_watch_sync").fetchone()[0] == 0


def test_the_cap_spreads_a_catch_up_over_passes(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    items = []
    for n in range(1, 6):
        add_episode(conn, f"e-aaaaa{n}", "s-aaaaaa", 1, n, "watched", "2026-03-04T20:30:00Z")
        items.append(jf_episode(f"J-E{n}", 1, n))
    fake = FakeJellyfin([series(42)], {"J-S1": items})
    first = run(conn, monkeypatch, fake, limit=3)
    assert first["marked_played"] == 3 and first["capped"] is True
    second = run(conn, monkeypatch, fake, limit=3)
    assert second["marked_played"] == 2 and second["capped"] is False


def test_in_capture_mode_nothing_is_recorded_as_agreed(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-03-04T20:30:00Z")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1)]})
    monkeypatch.setattr(external_writes, "capturing", lambda: True)
    run(conn, monkeypatch, fake)
    assert conn.execute("SELECT COUNT(*) FROM jellyfin_watch_sync").fetchone()[0] == 0


def test_not_configured_and_jellyfin_down_never_raise(conn, monkeypatch):
    config.set_current(config.Config())
    assert jellyfin_sync.run(conn)["configured"] is False
    config.set_current(config.Config(jellyfin_url="http://j", jellyfin_api_key="k",
                                     jellyfin_user="media"))
    fake = FakeJellyfin(fail=jellyfin_client.JellyfinError("down"))
    assert run(conn, monkeypatch, fake)["failed"] is True


# ── the client, over a mock transport ────────────────────────────────────────


def client_with(handler):
    http = httpx.Client(base_url="http://j", transport=httpx.MockTransport(handler),
                        headers={"Authorization": 'MediaBrowser Token="k"'})
    return jellyfin_client.JellyfinClient("http://j", "k", client=http)


def test_client_finds_the_user_lists_everything_and_marks(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.headers["authorization"] == 'MediaBrowser Token="k"'
        if request.url.path == "/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Name": "media"}])
        if request.url.path == "/Items":
            start = int(request.url.params["startIndex"])
            return httpx.Response(200, json={"Items": [{"Id": f"i{start}"}] if start < 2 else [],
                                             "TotalRecordCount": 2})
        return httpx.Response(204)

    monkeypatch.setattr(external_writes, "capturing", lambda: False)
    with client_with(handler) as client:
        assert client.user_id("MEDIA") == "u1"
        assert len(client.all_series("u1")) == 2  # paged
        client.mark_played("i0", "u1", "2026-03-04T20:30:00Z")
        client.mark_unplayed("i0", "u1")
    played_at = {"userId": "u1", "datePlayed": "2026-03-04T20:30:00Z"}
    assert ("POST", "/UserPlayedItems/i0", played_at) in calls
    assert ("DELETE", "/UserPlayedItems/i0", {"userId": "u1"}) in calls


def test_client_reports_a_rejected_key_and_an_unknown_user(monkeypatch):
    with client_with(lambda r: httpx.Response(401)) as client:
        with pytest.raises(jellyfin_client.JellyfinError, match="rejected the API key"):
            client.user_id("media")
    with client_with(lambda r: httpx.Response(200, json=[{"Id": "u1", "Name": "x"}])) as client:
        with pytest.raises(jellyfin_client.JellyfinError, match="no Jellyfin user"):
            client.user_id("media")


# ── phase 2: a play that exists only in Jellyfin becomes an LCARS watch ──────

SINCE = "2026-10-10T12:00:00Z"


def jf_played(item_id, season, number, last_played, end=None):
    item = jf_episode(item_id, season, number, played=True, end=end)
    item["UserData"]["LastPlayedDate"] = last_played
    return item


@pytest.fixture
def imports(monkeypatch):
    """The path an mpv watch takes, recorded instead of run."""
    from lcars import resolvers

    calls = []
    monkeypatch.setattr(resolvers, "resolve_add_watch_event",
                        lambda _, info, show_id, season, episode, when, platform:
                        calls.append(("watch", show_id, season, episode, when, platform)))
    monkeypatch.setattr(resolvers, "_apply_status_change",
                        lambda conn, show_id, status, by, confirmed=True:
                        calls.append(("status", show_id, status, by)))
    return calls


def test_a_recent_jellyfin_play_becomes_an_lcars_watch_at_its_time(conn, monkeypatch, imports):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    fake = FakeJellyfin(
        [series(42)], {"J-S1": [jf_played("J-E1", 1, 1, "2026-10-11T19:45:00.0000000Z")]})
    stats = run(conn, monkeypatch, fake, import_since=SINCE)
    assert imports == [("watch", "s-aaaaaa", 1, 1, "2026-10-11T19:45:00Z", "jellyfin")]
    assert stats["imported"] == 1 and stats["older_plays"] == 0 and fake.played == []
    row = conn.execute(
        "SELECT state FROM jellyfin_watch_sync WHERE entity_id = 'e-aaaaa1'").fetchone()
    assert row["state"] == "played"


def test_old_plays_are_counted_never_imported(conn, monkeypatch, imports):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_played("J-E1", 1, 1, "2024-05-01T10:00:00Z")]})
    stats = run(conn, monkeypatch, fake, import_since=SINCE)
    assert imports == [] and stats["older_plays"] == 1 and stats["imported"] == 0
    stats = run(conn, monkeypatch, fake)  # no start date at all: nothing is imported either
    assert imports == [] and stats["older_plays"] == 1


def test_a_dry_run_says_what_it_would_import_and_imports_nothing(conn, monkeypatch, imports):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_played("J-E1", 1, 1, "2026-10-11T19:45:00Z")]})
    stats = run(conn, monkeypatch, fake, dry_run=True, import_since=SINCE)
    assert stats["imported"] == 1 and imports == []
    assert conn.execute("SELECT COUNT(*) FROM jellyfin_watch_sync").fetchone()[0] == 0


def test_an_lcars_unwatch_of_an_agreed_play_is_unmarked_not_reimported(conn, monkeypatch, imports):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-10-11T10:00:00Z")
    played = jf_played("J-E1", 1, 1, "2026-10-11T19:45:00Z")
    fake = FakeJellyfin([series(42)], {"J-S1": [played]})
    run(conn, monkeypatch, fake, import_since=SINCE)  # agreed
    conn.execute("UPDATE episode SET state = 'unwatched' WHERE id = 'e-aaaaa1'")
    stats = run(conn, monkeypatch, fake, import_since=SINCE)
    assert fake.unplayed == ["J-E1"] and imports == [] and stats["imported"] == 0  # LCARS wins


def test_a_file_for_two_episodes_imports_only_the_unwatched_one(conn, monkeypatch, imports):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "watched", "2026-10-01T10:00:00Z")
    add_episode(conn, "e-aaaaa2", "s-aaaaaa", 1, 2, "unwatched")
    fake = FakeJellyfin([series(42)], {"J-S1": [
        jf_played("J-E12", 1, 1, "2026-10-11T19:45:00Z", end=2)]})
    run(conn, monkeypatch, fake, import_since=SINCE)
    assert imports == [("watch", "s-aaaaaa", 1, 2, "2026-10-11T19:45:00Z", "jellyfin")]


def test_a_recent_movie_play_completes_it_but_a_skipped_one_is_left(conn, monkeypatch, imports):
    add_show(conn, "s-movie1", 777, shape="movie", status="planned")
    add_show(conn, "s-movie2", 778, shape="movie", status="skipped")
    movies = [
        {"Id": "J-M1", "ProviderIds": {"Tmdb": "777"},
         "UserData": {"Played": True, "LastPlayedDate": "2026-10-11T21:00:00Z"}},
        {"Id": "J-M2", "ProviderIds": {"Tmdb": "778"},
         "UserData": {"Played": True, "LastPlayedDate": "2026-10-11T21:00:00Z"}}]
    stats = run(conn, monkeypatch, FakeJellyfin(movies=movies), import_since=SINCE)
    assert imports == [("watch", "s-movie1", None, None, "2026-10-11T21:00:00Z", "jellyfin"),
                       ("status", "s-movie1", "completed", "jellyfin")]
    assert stats["imported"] == 1


def test_imports_are_capped_per_pass(conn, monkeypatch, imports):
    monkeypatch.setattr(jellyfin_sync, "IMPORT_LIMIT", 2)
    add_show(conn, "s-aaaaaa", 42)
    items = []
    for n in range(1, 5):
        add_episode(conn, f"e-aaaaa{n}", "s-aaaaaa", 1, n, "unwatched")
        items.append(jf_played(f"J-E{n}", 1, n, "2026-10-11T19:45:00Z"))
    stats = run(conn, monkeypatch, FakeJellyfin([series(42)], {"J-S1": items}), import_since=SINCE)
    assert stats["imported"] == 2 and stats["capped"] is True and len(imports) == 2


def test_a_failed_import_is_reported_and_does_not_stop_the_pass(conn, monkeypatch):
    from lcars import resolvers

    def boom(*a, **k):
        raise RuntimeError("push failed")

    monkeypatch.setattr(resolvers, "resolve_add_watch_event", boom)
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    fake = FakeJellyfin([series(42)], {"J-S1": [jf_played("J-E1", 1, 1, "2026-10-11T19:45:00Z")]})
    stats = run(conn, monkeypatch, fake, import_since=SINCE)
    assert stats["failed"] is True and stats["imported"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jellyfin_watch_sync").fetchone()[0] == 0


# ── the Jellyfin links: which Jellyfin item is which ─────────────────────────


def items(conn):
    return {(r["kind"], r["entity_id"]): r["jellyfin_item_id"]
            for r in conn.execute("SELECT * FROM jellyfin_item")}


def test_the_sync_remembers_every_matched_item_even_with_nothing_to_mark(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    add_episode(conn, "e-aaaaa2", "s-aaaaaa", 1, 2, "watched", "2026-03-04T20:30:00Z")
    add_show(conn, "s-movie1", 777, shape="movie", status="planned")
    fake = FakeJellyfin([series(42, "J-S1")],
                        {"J-S1": [jf_episode("J-E1", 1, 1), jf_episode("J-E2", 1, 2, played=True)]},
                        movies=[{"Id": "J-M1", "ProviderIds": {"Tmdb": "777"},
                                 "UserData": {"Played": False}}])
    run(conn, monkeypatch, fake)
    assert items(conn) == {("show", "s-aaaaaa"): "J-S1", ("episode", "e-aaaaa1"): "J-E1",
                           ("episode", "e-aaaaa2"): "J-E2", ("show", "s-movie1"): "J-M1"}


def test_a_dry_run_remembers_nothing(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    run(conn, monkeypatch, FakeJellyfin([series(42)], {"J-S1": [jf_episode("J-E1", 1, 1)]}),
        dry_run=True)
    assert items(conn) == {}


def test_what_left_jellyfin_loses_its_link(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_show(conn, "s-bbbbbb", 43)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    add_episode(conn, "e-aaaaa2", "s-aaaaaa", 1, 2, "unwatched")
    add_episode(conn, "e-bbbbb1", "s-bbbbbb", 1, 1, "unwatched")
    both = FakeJellyfin([series(42, "J-S1"), series(43, "J-S2")], {
        "J-S1": [jf_episode("J-E1", 1, 1), jf_episode("J-E2", 1, 2)],
        "J-S2": [jf_episode("J-F1", 1, 1)]})
    run(conn, monkeypatch, both)
    assert len(items(conn)) == 5
    # the second episode's file and the whole second show are gone from Jellyfin
    run(conn, monkeypatch, FakeJellyfin([series(42, "J-S1")], {"J-S1": [jf_episode("J-E1", 1, 1)]}))
    assert items(conn) == {("show", "s-aaaaaa"): "J-S1", ("episode", "e-aaaaa1"): "J-E1"}


def test_a_single_show_pass_leaves_other_shows_links_alone(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", 42)
    add_show(conn, "s-bbbbbb", 43)
    add_episode(conn, "e-aaaaa1", "s-aaaaaa", 1, 1, "unwatched")
    add_episode(conn, "e-bbbbb1", "s-bbbbbb", 1, 1, "unwatched")
    fake = FakeJellyfin([series(42, "J-S1"), series(43, "J-S2")], {
        "J-S1": [jf_episode("J-E1", 1, 1)], "J-S2": [jf_episode("J-F1", 1, 1)]})
    run(conn, monkeypatch, fake)
    run(conn, monkeypatch, fake, show_id="s-aaaaaa")
    assert ("show", "s-bbbbbb") in items(conn) and ("episode", "e-bbbbb1") in items(conn)


def test_link_for_uses_the_episode_page_the_movie_page_and_the_public_url(conn):
    add_show(conn, "s-aaaaaa", 42)
    add_show(conn, "s-movie1", 777, shape="movie", status="planned")
    conn.execute("INSERT INTO jellyfin_item VALUES ('show', 's-aaaaaa', 'J-S1', ?)", (NOW,))
    conn.execute("INSERT INTO jellyfin_item VALUES ('episode', 'e-aaaaa1', 'J-E1', ?)", (NOW,))
    conn.execute("INSERT INTO jellyfin_item VALUES ('show', 's-movie1', 'J-M1', ?)", (NOW,))
    assert jellyfin_sync.link_for(conn, "s-aaaaaa", "e-aaaaa1") == "http://j/web/#/details?id=J-E1"
    # an episode Jellyfin does not have gets no link: the icon appears only when mpv can play too
    assert jellyfin_sync.link_for(conn, "s-aaaaaa", "e-other") is None
    # the show's own page, and a movie (whatever its episode row)
    assert jellyfin_sync.link_for(conn, "s-aaaaaa") == "http://j/web/#/details?id=J-S1"
    assert jellyfin_sync.link_for(conn, "s-movie1", "e-movie1") == "http://j/web/#/details?id=J-M1"
    assert jellyfin_sync.link_for(conn, "s-zzzzzz", "e-zzzzz1") is None
    config.set_current(
        config.Config(jellyfin_url="http://j", jellyfin_public_url="http://jf.lan:8096/"))
    assert jellyfin_sync.link_for(conn, "s-aaaaaa", "e-aaaaa1") == (
        "http://jf.lan:8096/web/#/details?id=J-E1")
    config.set_current(config.Config())
    assert jellyfin_sync.link_for(conn, "s-aaaaaa", "e-aaaaa1") is None  # Jellyfin not configured
