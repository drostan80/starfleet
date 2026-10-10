"""The Maintainerr tags (keep / ongoing / purge) LCARS keeps on Sonarr/Radarr (user, 2026-10-10)."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import arr_tags, config, radarr_client, sonarr_client, sonarr_sync

NOW = "2026-01-01T00:00:00Z"


class FakeArr:
    """Sonarr (tvdbId) or Radarr (tmdbId) with tags, applying editor calls like the real thing."""

    def __init__(self, items, tags=("keep",), fail=None, capture=False):
        self.items = list(items)
        self.tag_list = [{"id": 10 + i, "label": label} for i, label in enumerate(tags)]
        self.edits, self.created = [], []
        self.fail, self.capture = fail, capture

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _check(self):
        if self.fail:
            raise self.fail

    def tags(self):
        self._check()
        return [dict(t) for t in self.tag_list]

    def create_tag(self, label):
        self.created.append(label)
        if self.capture:
            return {"label": label, "id": None}
        made = {"id": 100 + len(self.tag_list), "label": label}
        self.tag_list.append(made)
        return made

    def edit_tags(self, ids, tag_ids, apply):
        self.edits.append((sorted(ids), list(tag_ids), apply))
        for item in self.items:
            if item["id"] in ids:
                for t in tag_ids:
                    if apply == "add" and t not in item["tags"]:
                        item["tags"].append(t)
                    if apply == "remove" and t in item["tags"]:
                        item["tags"].remove(t)

    def _find(self, field, value):
        self._check()
        return next((i for i in self.items if i[field] == value), None)

    def series_by_tvdb_id(self, tvdb_id):
        return self._find("tvdbId", tvdb_id)

    def movie_by_tmdb_id(self, tmdb_id):
        return self._find("tmdbId", tmdb_id)

    def all_series(self):
        self._check()
        return self.items

    def all_movies(self):
        self._check()
        return self.items

    def labels(self, item):
        by_id = {t["id"]: t["label"] for t in self.tag_list}
        return {by_id[i] for i in item["tags"]}


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("arr_tags") / "t.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    return path


@pytest.fixture
def conn(template, tmp_path):
    path = tmp_path / "s.db"
    shutil.copy(template, path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    config.set_current(config.Config(
        sonarr_url="http://s", sonarr_api_key="k", radarr_url="http://r", radarr_api_key="k"))
    yield c
    config.set_current(config.Config())
    c.close()


def add_show(conn, show_id, status, key, *, shape="episodic", tracked=1):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " tracked, created_at, updated_at) VALUES (?, ?, 'tv', ?, 'romaji', ?, ?, ?, ?)",
        (show_id, shape, show_id, status, tracked, NOW, NOW))
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, '', ?)",
        (show_id, "tmdb" if shape == "movie" else "tvdb", str(key), NOW))


def fake_sonarr(monkeypatch, items, **kw):
    fake = FakeArr(items, **kw)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **k: fake)
    return fake


def fake_radarr(monkeypatch, items, **kw):
    fake = FakeArr(items, **kw)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **k: fake)
    return fake


def series(item_id, tvdb, tags=(), slug="x"):
    return {"id": item_id, "tvdbId": tvdb, "titleSlug": slug, "tags": list(tags)}


# ── the rules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,current,add,remove", [
    ("planned", set(), {"ongoing"}, set()),
    ("watching", {"keep"}, {"ongoing"}, set()),
    ("paused", {"ongoing"}, set(), set()),
    ("completed", {"ongoing"}, set(), {"ongoing"}),
    ("completed", {"keep"}, set(), set()),
    ("dropped", set(), {"purge"}, set()),
    ("dropped", {"keep", "ongoing"}, {"purge"}, {"keep", "ongoing"}),  # purge wins over everything
    ("dropped", {"purge"}, set(), set()),
    ("skipped", {"ongoing", "purge"}, set(), {"ongoing", "purge"}),
    ("planned", {"purge"}, {"ongoing"}, {"purge"}),  # un-dropped
])
def test_plan_episodic(status, current, add, remove):
    assert arr_tags.plan(current, status, "episodic") == (add, remove)


def test_a_movie_is_never_ongoing_only_purged():
    assert arr_tags.plan(set(), "watching", "movie") == (set(), set())
    assert arr_tags.plan({"keep"}, "dropped", "movie") == ({"purge"}, {"keep"})


# ── syncing a show ───────────────────────────────────────────────────────────


def test_a_watching_show_gets_ongoing_and_the_tag_is_created(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "watching", 42)
    fake = fake_sonarr(monkeypatch, [series(1, 42)])
    stats = arr_tags.sync_shows(conn, ["s-aaaaaa"])
    assert fake.created == ["ongoing"] and stats["added"] == 1
    assert fake.labels(fake.items[0]) == {"ongoing"}
    assert fake.edits == [([1], [fake.tag_list[-1]["id"]], "add")]


def test_completed_removes_ongoing_and_leaves_keep(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "completed", 42)
    fake = fake_sonarr(monkeypatch, [series(1, 42, tags=[10, 11])], tags=("keep", "ongoing"))
    arr_tags.sync_shows(conn, ["s-aaaaaa"])
    assert fake.labels(fake.items[0]) == {"keep"}


def test_dropped_gets_purge_and_loses_keep_and_ongoing(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "dropped", 42)
    fake = fake_sonarr(monkeypatch, [series(1, 42, tags=[10, 11])], tags=("keep", "ongoing"))
    arr_tags.sync_shows(conn, ["s-aaaaaa"])
    assert fake.labels(fake.items[0]) == {"purge"}


def test_matched_by_tvdb_id_never_by_the_stored_sonarr_slug(conn, monkeypatch):
    # Hamatora held The Sandman's slug: the tag must follow the TVDB id, not the slug.
    add_show(conn, "s-hamham", "dropped", 275634)
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES ('s-hamham', 'sonarr', 'the-sandman', '', ?)", (NOW,))
    sandman = series(1, 366211, tags=[10], slug="the-sandman")
    fake = fake_sonarr(monkeypatch, [sandman])
    stats = arr_tags.sync_shows(conn, ["s-hamham"])
    assert stats["not_in_arr"] == 1 and fake.edits == []
    assert fake.labels(sandman) == {"keep"}


def test_untracked_and_ambiguous_shows_are_left_alone(conn, monkeypatch):
    add_show(conn, "s-gone00", "dropped", 1, tracked=0)
    add_show(conn, "s-d10000", "dropped", 2)
    conn.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
                 " status, tracked, created_at, updated_at) VALUES ('s-d20000', 'episodic', 'tv',"
                 " 'd2', 'romaji', 'dropped', 1, ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES ('s-d20000', 'tvdb', '2', '', ?)", (NOW,))
    fake = fake_sonarr(monkeypatch, [series(1, 1), series(2, 2)])
    arr_tags.sync_shows(conn, ["s-gone00", "s-d10000", "s-d20000"])
    assert fake.edits == []


def test_a_service_error_is_recorded_and_never_raised(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "watching", 42)
    fake_sonarr(monkeypatch, [series(1, 42)], fail=sonarr_client.SonarrError("down"))
    assert arr_tags.sync_shows(conn, ["s-aaaaaa"])["failed"] is True


def test_an_unexpected_bug_is_logged_and_never_raised(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "watching", 42)
    fake_sonarr(monkeypatch, [series(1, 42)], fail=RuntimeError("boom"))
    assert arr_tags.sync_shows(conn, ["s-aaaaaa"])["failed"] is True


def test_a_captured_tag_create_has_no_id_so_nothing_is_edited(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "watching", 42)
    fake = fake_sonarr(monkeypatch, [series(1, 42)], capture=True)
    arr_tags.sync_shows(conn, ["s-aaaaaa"])
    assert fake.created == ["ongoing"] and fake.edits == []


def test_a_dropped_movie_gets_purge_in_radarr(conn, monkeypatch):
    add_show(conn, "s-movie1", "dropped", 777, shape="movie")
    add_show(conn, "s-movie2", "planned", 778, shape="movie")
    movies = [{"id": 5, "tmdbId": 777, "tags": [10]}, {"id": 6, "tmdbId": 778, "tags": []}]
    fake = fake_radarr(monkeypatch, movies)
    arr_tags.sync_shows(conn, ["s-movie1", "s-movie2"])
    assert fake.labels(movies[0]) == {"purge"} and movies[1]["tags"] == []


# ── the hourly pass ──────────────────────────────────────────────────────────


def test_reconcile_fixes_the_whole_library_in_grouped_calls_and_is_idempotent(conn, monkeypatch):
    for n in range(1, 5):
        add_show(conn, f"s-w0000{n}", "watching", n)
    add_show(conn, "s-cccccc", "completed", 10)
    add_show(conn, "s-dddddd", "dropped", 20)
    items = [series(n, n) for n in range(1, 5)] + [
        series(10, 10, tags=[11]), series(20, 20, tags=[10]), series(99, 99)]
    fake = fake_sonarr(monkeypatch, items, tags=("keep", "ongoing"))
    first = arr_tags.reconcile(conn)
    assert first["episodic"] == {"added": 5, "removed": 2}
    assert len(fake.edits) == 4  # ongoing +4, purge +1, ongoing -1 (s-c), keep -1 (s-d)
    assert fake.labels(items[4]) == set() and fake.labels(items[5]) == {"purge"}
    assert fake.labels(items[6]) == set()  # a series LCARS does not track is untouched
    edits_before = len(fake.edits)
    assert arr_tags.reconcile(conn)["episodic"] == {"added": 0, "removed": 0}
    assert len(fake.edits) == edits_before


# ── keep ─────────────────────────────────────────────────────────────────────


def test_keep_toggle_and_state(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "completed", 42)
    fake = fake_sonarr(monkeypatch, [series(1, 42)])
    assert arr_tags.keep_state(conn, "s-aaaaaa") is False
    assert arr_tags.set_keep(conn, "s-aaaaaa", True) is True
    assert arr_tags.keep_state(conn, "s-aaaaaa") is True and fake.labels(fake.items[0]) == {"keep"}
    arr_tags.set_keep(conn, "s-aaaaaa", False)
    assert arr_tags.keep_state(conn, "s-aaaaaa") is False


def test_a_dropped_show_cannot_be_kept(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "dropped", 42)
    fake_sonarr(monkeypatch, [series(1, 42)])
    with pytest.raises(arr_tags.ArrTagError, match="purge wins over keep"):
        arr_tags.set_keep(conn, "s-aaaaaa", True)
    arr_tags.set_keep(conn, "s-aaaaaa", False)  # taking keep off is always fine


def test_keep_on_a_show_not_in_sonarr_says_so(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "completed", 42)
    fake_sonarr(monkeypatch, [])
    with pytest.raises(arr_tags.ArrTagError, match="not in Sonarr"):
        arr_tags.set_keep(conn, "s-aaaaaa", True)
    assert arr_tags.keep_state(conn, "s-aaaaaa") is None


# ── the hook ─────────────────────────────────────────────────────────────────


def test_every_status_change_syncs_the_tags_of_its_shows_completed_included(conn, monkeypatch):
    add_show(conn, "s-aaaaaa", "completed", 42)
    conn.execute("INSERT INTO season (id, show_id, season_number, source, status, created_at,"
                 " updated_at) VALUES ('z-aaaaa1', 's-aaaaaa', 1, 'manual', 'completed', ?, ?)",
                 (NOW, NOW))
    seen = []
    monkeypatch.setattr(arr_tags, "sync_shows", lambda c, ids: seen.append(set(ids)))
    sonarr_sync.apply(conn, [("z-aaaaa1", "watching", "completed")])
    assert seen == [{"s-aaaaaa"}]
