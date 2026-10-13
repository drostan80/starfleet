"""PLAN-CODE phase 10 (user, 2026-10-13): TheTVDB deleted four shows and re-created them under new
ids. A Sonarr series with status `deleted` that a tracked show links to becomes a review with the
evidence; replacing changes the Sonarr series' TVDB id in place and LCARS's link together, and puts
Sonarr back when a check after it fails. Nothing here talks to a real Sonarr."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import config, reviews, sonarr_client, tvdb_recheck, tvdb_replace, tvdb_vetting

OLD, NEW = 468049, 483424
NOW = "2026-10-13T00:00:00Z"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    config.set_current(config.Config())
    monkeypatch.setattr(tvdb_vetting, "show_entry_facts",
                        lambda conn, sid: {"anilist_id": 1, "titles": ["Victoria of Many Faces"],
                                           "year": 2026, "country": "JP", "format": "TV",
                                           "episodes": 9, "poster": None})
    yield


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "replace.db"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=Path(__file__).resolve().parent.parent,
                   env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
                   check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("INSERT INTO show (id, media_shape, tracking_space, title_english, primary_title,"
              " status, tracked, created_at, updated_at) VALUES ('s-victor', 'episodic', 'anime',"
              " 'Victoria of Many Faces', 'english', 'planned', 1, ?, ?)", (NOW, NOW))
    for service, value, source in (("tvdb", OLD, "agreed:x"), ("imdb", "tt38273525", None),
                                   ("tmdb", 302519, None),
                                   ("sonarr", "victoria-of-many-faces", None)):
        c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at,"
                  " source) VALUES ('s-victor', ?, ?, '', ?, ?)",
                  (service, str(value), NOW, source))
    c.commit()
    return c


def _series(tvdb=OLD, status="deleted", files=9):
    return {"id": 1072, "tvdbId": tvdb, "title": "Victoria of Many Faces", "year": 2026,
            "titleSlug": "victoria-of-many-faces",
            "status": status, "monitored": True, "tags": [25, 26],
            "path": "/data/media/anime/Victoria of Many Faces (2026) {tvdb-468049}",
            "statistics": {"episodeFileCount": files, "episodeCount": files},
            "seasons": [{"seasonNumber": 1, "monitored": True,
                         "statistics": {"episodeFileCount": files, "totalEpisodeCount": files}}]}


def _found(tvdb=NEW, imdb="tt38273525", tmdb=302519, title="Victoria of Many Faces", year=2026,
           in_library=None, seasons=(1, 2)):
    found = {"tvdbId": tvdb, "imdbId": imdb, "tmdbId": tmdb, "title": title, "year": year,
             "genres": ["Animation"], "originalLanguage": {"name": "Japanese"},
             "seasons": [{"seasonNumber": n} for n in seasons]}
    if in_library:
        found["id"] = in_library
    return found


class FakeSonarr:
    """Just enough of Sonarr: the library, lookup, an edit that sticks, a refresh that fixes the
    status when TVDB knows the id."""

    def __init__(self, series, lookups, refresh_ok=True, lose_files=False):
        self.library = {series["id"]: series}
        self.lookups = lookups  # term -> [found]
        self.refresh_ok, self.lose_files = refresh_ok, lose_files
        self.puts = []

    def close(self): ...

    def tags(self):
        return [{"id": 25, "label": "keep"}, {"id": 26, "label": "ongoing"}]

    def all_series(self):
        return copy.deepcopy(list(self.library.values()))

    def series_by_tvdb_id(self, tvdb):
        for s in self.library.values():
            if s["tvdbId"] == tvdb:
                return copy.deepcopy(s)
        return None

    def series(self, sid):
        return copy.deepcopy(self.library[sid])

    def lookup_series(self, term):
        if term.startswith("tvdb:"):
            wanted = int(term[5:])
            return [f for fs in self.lookups.values() for f in fs if f["tvdbId"] == wanted][:1]
        return copy.deepcopy(self.lookups.get(term, []))

    def update_series(self, series):
        self.puts.append(copy.deepcopy(series))
        self.library[series["id"]] = copy.deepcopy(series)
        return series

    def refresh_series(self, sid, wait_seconds=90.0):
        if not self.refresh_ok:
            raise sonarr_client.SonarrError("Sonarr's refresh failed: boom")
        s = self.library[sid]
        s["status"] = "continuing"
        if self.lose_files:
            s["statistics"]["episodeFileCount"] = 0
        return {"status": "completed"}


def _fake(**kw):
    found = _found()
    lookups = {"imdb:tt38273525": [found], "tmdb:302519": [found],
               "Victoria of Many Faces": [found]}
    return FakeSonarr(_series(), lookups, **kw)


def _open(conn):
    return conn.execute("SELECT * FROM pending_review WHERE field = ? AND resolved_at IS NULL",
                        (tvdb_replace.FIELD,)).fetchall()


def _tvdb(conn):
    return conn.execute("SELECT external_id, source FROM show_external_id WHERE show_id ="
                        " 's-victor' AND service = 'tvdb'").fetchone()


def test_a_deleted_series_opens_one_review_with_the_evidence_and_not_again(conn):
    sonarr = _fake()
    assert tvdb_replace.scan(conn, sonarr) == 1
    [r] = _open(conn)
    assert [c["id"] for c in json.loads(r["choices"])] == ["replace_tvdb", "replace_other_tvdb",
                                                          "log_no_tvdb"]
    p = json.loads(r["payload"])
    assert p["proposed"]["tvdb_id"] == NEW and not p["proposed"]["blocked"]
    assert p["sonarr"]["tags"] == ["keep", "ongoing"] and p["sonarr"]["files"] == 9
    assert "IMDb tt38273525 matches" in p["checks"] and "title fits" in p["checks"]
    assert [c["tvdb_id"] for c in p["columns"]] == [OLD, NEW]
    assert tvdb_replace.scan(conn, sonarr) == 0  # the same finding is not asked twice
    assert sonarr.puts == [] and _tvdb(conn)["external_id"] == str(OLD)  # nothing changed


def test_a_series_that_is_fine_or_not_tracked_opens_nothing(conn):
    ok = _fake()
    ok.library[1072]["status"] = "continuing"
    assert tvdb_replace.scan(conn, ok) == 0
    other = _fake()
    other.library[1072]["tvdbId"] = 999  # no tracked show links TVDB 999 or this slug
    other.library[1072]["titleSlug"] = "something-else"
    assert tvdb_replace.scan(conn, other) == 0


def test_replacing_changes_sonarr_in_place_and_lcars_together(conn):
    sonarr = _fake()
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    summary = tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)
    assert "9 → 9" in summary and "deleted → continuing" in summary
    s = sonarr.library[1072]
    assert s["tvdbId"] == NEW and s["tags"] == [25, 26] and s["path"].endswith("{tvdb-468049}")
    row = _tvdb(conn)
    assert (row["external_id"], row["source"]) == (str(NEW), "you")  # your choice: never rechecked
    assert r["id"]


def test_the_review_choice_applies_and_keeps_the_result_with_a_backup(conn, monkeypatch):
    sonarr = _fake()
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    monkeypatch.setattr(tvdb_replace.sonarr_client, "SonarrClient", lambda *a, **k: sonarr)
    config.set_current(config.Config(sonarr_url="http://x", sonarr_api_key="k"))
    reviews.resolve_choice(conn, r["id"], "replace_tvdb", "holodeck", None)
    done = conn.execute("SELECT * FROM pending_review WHERE id = ?", (r["id"],)).fetchone()
    assert done["resolved_at"] and "9 → 9" in done["resolution_note"]
    assert json.loads(done["payload"])["backup"]["tvdbId"] == OLD
    assert _tvdb(conn)["external_id"] == str(NEW)


@pytest.mark.parametrize("kw,why", [({"refresh_ok": False}, "refresh failed"),
                                    ({"lose_files": True}, "files fell")])
def test_a_failed_check_puts_sonarr_back_and_changes_nothing_in_lcars(conn, kw, why):
    sonarr = _fake(**kw)
    with pytest.raises(reviews.ReviewError) as e:
        tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)
    assert "Sonarr put back as it was" in str(e.value) and why in str(e.value)
    assert sonarr.library[1072]["tvdbId"] == OLD and sonarr.library[1072]["tags"] == [25, 26]
    assert _tvdb(conn)["external_id"] == str(OLD)


def test_an_lcars_failure_after_sonarr_changed_puts_sonarr_back(conn):
    sonarr = _fake()
    conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON show_external_id"
                 " BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    with pytest.raises(reviews.ReviewError) as e:
        tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)
    assert "couldn't record the new id" in str(e.value) and "put back" in str(e.value)
    assert sonarr.library[1072]["tvdbId"] == OLD


def test_the_refusals_before_anything_is_touched(conn):
    sonarr = _fake()
    conn.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
                 " status, tracked, created_at, updated_at) VALUES ('s-other1', 'episodic',"
                 " 'anime', 'x', 'romaji', 'planned', 1, ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 f" VALUES ('s-other1', 'tvdb', '{NEW}', '', ?)", (NOW,))
    with pytest.raises(reviews.ReviewError, match="R1.14"):
        tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)
    conn.execute("DELETE FROM show_external_id WHERE show_id = 's-other1'")
    sonarr.library[2000] = _series(NEW) | {"id": 2000}
    with pytest.raises(reviews.ReviewError, match="already has a series"):
        tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)
    del sonarr.library[2000]
    sonarr.lookups = {"x": [_found(seasons=(2,))]}  # the new entry has no season 1: files are in it
    with pytest.raises(reviews.ReviewError, match="numbers the show differently"):
        tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)
    assert sonarr.puts == [] and _tvdb(conn)["external_id"] == str(OLD)


def test_capture_mode_refuses_instead_of_recording_a_change_that_never_happens(conn, monkeypatch):
    monkeypatch.setenv("LCARS_EXTERNAL_WRITES", "capture")
    with pytest.raises(reviews.ReviewError, match="capture mode"):
        tvdb_replace.apply(conn, "s-victor", OLD, NEW, _fake())
    assert _tvdb(conn)["external_id"] == str(OLD)


def test_a_year_mismatch_needs_the_despite_choice_and_the_plain_one_is_refused(conn, monkeypatch):
    sonarr = _fake()
    monkeypatch.setattr(tvdb_vetting, "hard_stops", lambda *a, **k: ["first aired 2019, ..."])
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    assert json.loads(r["choices"])[0]["id"] == "replace_tvdb_despite"
    with pytest.raises(reviews.ReviewError):  # not offered
        reviews.resolve_choice(conn, r["id"], "replace_tvdb", "holodeck", None)
    assert _tvdb(conn)["external_id"] == str(OLD)


def test_no_candidate_still_opens_a_review_you_can_answer_with_an_id(conn):
    sonarr = FakeSonarr(_series(), {})
    assert tvdb_replace.scan(conn, sonarr) == 1
    [r] = _open(conn)
    assert [c["id"] for c in json.loads(r["choices"])] == ["log_no_tvdb", "replace_other_tvdb"]
    assert json.loads(r["payload"])["proposed"] is None


def test_a_conflicting_id_is_not_a_candidate(conn):
    wrong = _found(imdb="tt0000001")  # present on both sides and different
    sonarr = FakeSonarr(_series(), {"imdb:tt38273525": [wrong], "Victoria of Many Faces": [wrong]})
    tvdb_replace.scan(conn, sonarr)
    assert json.loads(_open(conn)[0]["payload"])["proposed"] is None


def test_a_candidate_already_in_sonarr_is_shown_but_cannot_be_applied(conn):
    found = _found(in_library=777)
    sonarr = FakeSonarr(_series(), {"imdb:tt38273525": [found]})
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    assert "replace_tvdb" not in [c["id"] for c in json.loads(r["choices"])]
    assert "series 777" in json.loads(r["payload"])["proposed"]["blocked"]


def test_the_record_the_reason_and_the_proposal_are_all_in_the_review(conn):
    tvdb_replace.scan(conn, _fake())
    [r] = _open(conn)
    p = json.loads(r["payload"])
    assert p["lcars"]["tvdb_id"] == str(OLD) and p["lcars"]["title"] == "Victoria of Many Faces"
    assert p["lcars"]["imdb"] == "tt38273525" and p["lcars"]["sonarr_slug"]
    assert json.loads(r["proposed_value_chain"])[-1].startswith(f"Sonarr flagged TVDB {OLD}")


def test_logging_without_an_id_removes_the_stale_one_leaves_sonarr_and_is_remembered(conn):
    sonarr = FakeSonarr(_series(), {})  # nothing to propose
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    assert "no replacement TVDB id is available" in json.loads(r["proposed_value_chain"])[-1]
    reviews.resolve_choice(conn, r["id"], "log_no_tvdb", "holodeck", None)
    assert _tvdb(conn) is None  # a stale id is worse than none
    assert sonarr.puts == [] and sonarr.library[1072]["tvdbId"] == OLD
    assert tvdb_replace.scan(conn, sonarr) == 0 and not _open(conn)  # same finding: not asked again


def test_a_show_without_an_id_is_found_by_slug_and_asked_again_when_a_proposal_exists(conn):
    sonarr = FakeSonarr(_series(), {})
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    reviews.resolve_choice(conn, r["id"], "log_no_tvdb", "holodeck", None)
    sonarr.lookups = {"imdb:tt38273525": [_found()]}  # now TVDB has it
    assert tvdb_replace.scan(conn, sonarr) == 1  # a different finding: a new review
    [r2] = _open(conn)
    assert json.loads(r2["payload"])["lcars"]["tvdb_id"] is None
    tvdb_replace.apply(conn, "s-victor", OLD, NEW, sonarr)  # no id yet: it gets the new one
    assert _tvdb(conn)["external_id"] == str(NEW) and sonarr.library[1072]["tvdbId"] == NEW


def test_an_id_thetvdb_deleted_is_not_offered_back_by_the_sources(conn):
    from lcars import tvdb_guard

    tvdb_replace.scan(conn, _fake())  # records that OLD was flagged
    conn.execute("DELETE FROM show_external_id WHERE show_id = 's-victor' AND service = 'tvdb'")
    assert tvdb_guard.offer(conn, "s-victor", OLD, "tvmaze") == tvdb_guard.DELETED
    assert tvdb_guard.offer(conn, "s-victor", OLD, "wikidata") == tvdb_guard.DELETED
    assert _tvdb(conn) is None
    assert tvdb_guard.offer(conn, "s-victor", OLD, "you", confirmed=True) == tvdb_guard.WRITTEN


def test_a_deleted_sonarr_series_is_not_an_untracked_show():
    from lcars import local_audit

    series = [{"title": "A", "tvdbId": 1, "status": "deleted", "seasons": []},
              {"title": "B", "tvdbId": 2, "status": "continuing", "seasons": []}]
    assert [e["title"] for e in local_audit._untracked_sonarr_entries(series, set())] == ["B"]


def test_your_own_id_in_the_note_is_checked_and_a_misfit_reopens_with_the_despite_choice(
        conn, monkeypatch):
    sonarr = _fake()
    tvdb_replace.scan(conn, sonarr)
    [r] = _open(conn)
    monkeypatch.setattr(tvdb_vetting, "tvdb_facts",
                        lambda c, n: {"tvdb_id": n, "title": "Other", "year": 2010, "titles": [],
                                      "genres": [], "language": "English", "seasons": [1]})
    with pytest.raises(reviews.ReviewError) as e:
        reviews.resolve_choice(conn, r["id"], "replace_other_tvdb", "holodeck", "TVDB 123456")
    assert "doesn't fit" in str(e.value) and e.value.reopen["replace_review"] == r["id"]
    tvdb_replace.reopen(conn, e.value.reopen)
    [r2] = _open(conn)
    assert json.loads(r2["payload"])["proposed"]["tvdb_id"] == 123456
    assert json.loads(r2["choices"])[0]["id"] == "replace_tvdb_despite"


def test_auto_mode_applies_what_everything_agrees_on_and_keeps_a_resolved_review(
        conn, monkeypatch):
    sonarr = _fake()
    config.set_current(config.Config(tvdb_replace="auto", sonarr_url="http://x",
                                     sonarr_api_key="k"))
    monkeypatch.setattr(tvdb_replace.sonarr_client, "SonarrClient", lambda *a, **k: sonarr)
    assert tvdb_replace.scan(conn, sonarr) == 1
    assert _tvdb(conn)["external_id"] == str(NEW) and not _open(conn)
    done = conn.execute("SELECT * FROM pending_review WHERE field = 'tvdb_replace'").fetchone()
    assert done["resolved_by_client"] is None and "automatically" in done["resolution_note"]
    assert _tvdb(conn)["source"] == f"recreated:{OLD}"


def test_auto_mode_that_cannot_apply_keeps_the_review_open(conn, monkeypatch):
    sonarr = _fake(refresh_ok=False)
    config.set_current(config.Config(tvdb_replace="auto", sonarr_url="http://x",
                                     sonarr_api_key="k"))
    monkeypatch.setattr(tvdb_replace.sonarr_client, "SonarrClient", lambda *a, **k: sonarr)
    assert tvdb_replace.scan(conn, sonarr) == 1
    assert len(_open(conn)) == 1 and _tvdb(conn)["external_id"] == str(OLD)
    assert sonarr.library[1072]["tvdbId"] == OLD


def test_auto_mode_leaves_a_title_only_match_for_you(conn):
    found = _found(imdb=None, tmdb=None)
    sonarr = FakeSonarr(_series(), {"Victoria of Many Faces": [found]})
    config.set_current(config.Config(tvdb_replace="auto"))
    tvdb_replace.scan(conn, sonarr)
    assert len(_open(conn)) == 1 and _tvdb(conn)["external_id"] == str(OLD)


def test_off_does_not_look(conn):
    config.set_current(config.Config(tvdb_replace="off"))
    assert tvdb_replace.scan(conn, _fake()) == 0 and not _open(conn)


def test_fribb_still_giving_the_deleted_id_is_not_a_disagreement_but_another_id_is(conn):
    conn.execute("UPDATE show_external_id SET external_id = ?, source = ? WHERE service = 'tvdb'",
                 (str(NEW), f"recreated:{OLD}"))
    conn.execute("INSERT INTO season (id, show_id, season_number, anilist_id, source, status,"
                 " created_at, updated_at) VALUES ('z-aaaaaa', 's-victor', 1, 10, 'auto',"
                 " 'planned', ?, ?)", (NOW, NOW))
    fribb_old = [{"anilist_id": 10, "tvdb_id": OLD, "type": "TV", "season": {"tvdb": 1}}]
    assert tvdb_recheck.recheck(conn, fribb_old) == 0
    fribb_other = [{"anilist_id": 10, "tvdb_id": 999, "type": "TV", "season": {"tvdb": 1}}]
    assert tvdb_recheck.recheck(conn, fribb_other) == 1


def test_a_show_that_has_aired_fully_or_was_dropped_is_covered_too(conn):
    """User 2026-10-13: a finished show may get a season, a dropped one may be un-dropped — a
    stale TVDB id is never acceptable, history or not."""
    conn.execute("INSERT INTO season (id, show_id, season_number, source, status, created_at,"
                 " updated_at, kind) VALUES ('z-hist01', 's-victor', 1, 'auto', 'dropped', ?, ?,"
                 " 'tvdb_season')", (NOW, NOW))
    conn.execute("INSERT INTO episode (id, show_id, season_id, season, episode, kind, air_date_utc,"
                 " created_at, updated_at) VALUES ('e-hist01', 's-victor', 'z-hist01', 1, 1,"
                 " 'regular', '2025-01-01T00:00:00Z', ?, ?)", (NOW, NOW))
    conn.execute("UPDATE show SET status = 'dropped' WHERE id = 's-victor'")
    assert tvdb_replace.scan(conn, _fake()) == 1


def test_the_guard_refuses_a_replacement_the_rules_forbid(conn):
    from lcars import tvdb_guard

    with pytest.raises(reviews.ReviewError, match="now, not"):
        tvdb_guard.replace(conn, "s-victor", 999, NEW, "you")
    conn.execute("UPDATE show SET media_shape = 'movie' WHERE id = 's-victor'")
    with pytest.raises(reviews.ReviewError, match="R3.2a"):
        tvdb_guard.replace(conn, "s-victor", OLD, NEW, "you")
    with pytest.raises(reviews.ReviewError, match="not a TVDB series id"):
        tvdb_guard.replace(conn, "s-victor", OLD, "abc", "you")
