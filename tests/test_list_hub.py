"""The list hub (PLAN-CODE 7.5, RULEBOOK R4.9/R4.10, 2026-09-29): LCARS is the
truth and remembers what it wrote; an unchanged update time is no outside change;
a row settles before another outside change is taken; a tie goes to LCARS.

The fake lists keep an update clock like the real services: every write and every
"edit by hand" bumps the entry's own update time."""

import datetime
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lcars import (
    anilist_client,
    config,
    list_baseline,
    list_hub,
    mal_client,
    mal_reconcile,
    reviews,
    watch_reconcile,
)

PAST = "2020-01-01T00:00:00Z"
T0 = 1_760_000_000


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "list_hub.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    config.set_current(config.Config(
        anilist_access_token="a", mal_access_token="m", list_intake_enabled=True))
    yield c
    config.set_current(config.Config())


@pytest.fixture
def world(monkeypatch):
    clock = [T0]
    w = SimpleNamespace(state={"anilist": {}, "mal": {}}, calls=[], down={"anilist": False,
                        "mal": False}, clamp_mal_to=None, episodes=None)

    def tick():
        clock[0] += 10
        return clock[0]

    def put(service, ext, status, progress):
        w.state[service][ext] = {"status": status, "progress": progress, "t": tick()}

    def edit(service, ext, **fields):
        """A change made by hand on the list: the service stamps its own time."""
        w.state[service][ext].update(fields)
        w.state[service][ext]["t"] = tick()

    def al_save(token, media_id, **kw):
        if w.down["anilist"]:
            raise anilist_client.AniListError("down")
        w.calls.append(("anilist", media_id, kw))
        e = w.state["anilist"].setdefault(media_id, {"status": "PLANNING", "progress": 0})
        e.update({k: v for k, v in kw.items() if k in ("status", "progress")})
        if w.episodes and e["progress"] >= w.episodes and "status" not in kw:
            e["status"] = "COMPLETED"  # the service's own side effect
        e["t"] = tick()
        return {"id": 1, "status": e["status"], "progress": e["progress"], "updatedAt": e["t"]}

    def mal_save(token, mal_id, **kw):
        if w.down["mal"]:
            raise mal_client.MALError("down")
        w.calls.append(("mal", mal_id, kw))
        e = w.state["mal"].setdefault(mal_id, {"status": "plan_to_watch", "progress": 0})
        if "status" in kw:
            e["status"] = kw["status"]
        if "num_watched_episodes" in kw:
            e["progress"] = kw["num_watched_episodes"]
            if w.clamp_mal_to is not None:
                e["progress"] = min(e["progress"], w.clamp_mal_to)
        e["t"] = tick()
        stamp = datetime.datetime.fromtimestamp(e["t"], datetime.UTC).isoformat()
        return {"status": e["status"], "num_episodes_watched": e["progress"],
                "updated_at": stamp}

    monkeypatch.setattr(anilist_client, "save_media_list_entry", al_save)
    monkeypatch.setattr(mal_client, "update_my_list_status", mal_save)
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token: [
        {"anilist_id": k, "status": v["status"], "progress": v["progress"], "format": "TV",
         "updated_at": anilist_client._unix_to_iso(v["t"]), "episodes": v.get("episodes")}
        for k, v in w.state["anilist"].items()])
    monkeypatch.setattr(mal_client, "fetch_my_list", lambda token: [
        {"mal_id": k, "status": v["status"], "num_watched_episodes": v["progress"],
         "updated_at": mal_client._iso_utc(
             datetime.datetime.fromtimestamp(v["t"], datetime.UTC).isoformat())}
        for k, v in w.state["mal"].items()])
    w.put, w.edit, w.tick = put, edit, tick
    return w


def _show(conn, season_status="watching", episodes=5, watched=0):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-hub001', 'episodic', 'anime', 'T', 'romaji', ?, 1, 'x', 'x')",
        (season_status,))
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id, mal_id, source,"
        " created_at, updated_at) VALUES ('z-hub001', 's-hub001', 1, ?, 100, 200, 'fribb',"
        " 'x', 'x')", (season_status,))
    for service, ext in (("anilist", 100), ("mal", 200)):
        conn.execute(
            "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
            " VALUES ('z-hub001', ?, ?, 'x')", (service, ext))
    for n in range(1, episodes + 1):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, state, air_date_utc,"
            " created_at, updated_at) VALUES (?, 's-hub001', 1, ?, 'regular', ?, ?, 'x', 'x')",
            (f"e-hub00{n}", n, "watched" if n <= watched else "unwatched", PAST))
    conn.commit()


def _status(conn):
    return conn.execute("SELECT status FROM season WHERE id = 'z-hub001'").fetchone()[0]


def _watched(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM episode WHERE show_id = 's-hub001' AND state = 'watched'"
    ).fetchone()[0]


def _poll(conn):
    watch_reconcile.reconcile_watch_progress(conn)
    mal_reconcile.reconcile_mal_progress(conn)


def _seeded(conn, w, status="watching", al="CURRENT", mal="watching", watched=0, progress=0):
    _show(conn, status, watched=watched)
    w.put("anilist", 100, al, progress)
    w.put("mal", 200, mal, progress)
    _poll(conn)  # seed: the lists' values are what both agree on
    w.calls.clear()


def _log(conn, kind):
    return conn.execute("SELECT COUNT(*) FROM list_sync_log WHERE kind = ?", (kind,)).fetchone()[0]


# ── echo and read-back ────────────────────────────────────────────────────


def test_an_lcars_write_is_never_taken_back_as_an_outside_change(conn, world):
    _seeded(conn, world)
    conn.execute("UPDATE season SET status = 'paused' WHERE id = 'z-hub001'")
    from lcars import list_sync

    list_sync.push(conn, "z-hub001")
    conn.commit()
    world.calls.clear()
    for _ in range(3):
        _poll(conn)
    assert world.calls == []
    assert _status(conn) == "paused"
    base = list_baseline.get(conn, "anilist", 100)
    assert base["remote_updated_at"] == anilist_client._unix_to_iso(
        world.state["anilist"][100]["t"])  # the read-back time is the list's own
    assert not list_hub.locked(conn, "z-hub001")


def test_a_service_that_clamps_progress_is_left_with_one_write_and_one_review(conn, world):
    _seeded(conn, world, watched=0)
    world.clamp_mal_to = 2  # MAL keeps at most 2
    conn.execute("UPDATE episode SET state = 'watched' WHERE episode <= 4")
    from lcars import list_sync

    list_sync.push(conn, "z-hub001", status=False)
    conn.commit()
    mal_writes = [c for c in world.calls if c[0] == "mal"]
    assert len(mal_writes) == 1  # sent 4, MAL kept 2: no second push of the same value
    world.calls.clear()
    for _ in range(3):
        _poll(conn)
    assert [c for c in world.calls if c[0] == "mal"] == []
    review = conn.execute(
        "SELECT * FROM pending_review WHERE field = 'list_readback_differs'").fetchone()
    assert review is not None and "progress 2" in review["proposed_value_chain"]


def test_a_status_the_service_moved_itself_gets_one_corrective_write_then_a_review(
        conn, world):
    _seeded(conn, world, status="watching", al="CURRENT", watched=0)
    world.episodes = 4  # AniList completes an entry at 4 by itself
    conn.execute("UPDATE episode SET state = 'watched' WHERE episode <= 4")
    from lcars import list_sync

    list_sync.push(conn, "z-hub001", status=False, services=("anilist",))
    conn.commit()
    statuses = [c[2].get("status") for c in world.calls if c[0] == "anilist"]
    assert statuses == [None, "CURRENT"]  # progress push, then one status-only correction
    assert world.state["anilist"][100]["status"] == "CURRENT"


# ── outside edits: taken once, ordered by their real time ────────────────


def test_an_outside_edit_is_taken_and_reaches_the_other_list_once(conn, world):
    _seeded(conn, world)
    world.edit("mal", 200, status="on_hold")
    _poll(conn)
    assert _status(conn) == "paused"
    assert world.state["anilist"][100]["status"] == "PAUSED"
    world.calls.clear()
    for _ in range(3):
        _poll(conn)
    assert world.calls == []
    assert not list_hub.locked(conn, "z-hub001")  # every list agrees: settled


def test_two_lists_edited_at_once_the_later_real_edit_wins_and_nothing_is_overwritten(
        conn, world):
    _seeded(conn, world)
    world.edit("anilist", 100, status="PAUSED")      # earlier
    world.edit("mal", 200, status="dropped")         # later, before LCARS looked at either
    watch_reconcile.reconcile_watch_progress(conn)   # takes the AniList edit ...
    assert _status(conn) == "paused"
    # ... but MAL was edited since LCARS last looked: its edit is held, not overwritten
    assert world.state["mal"][200]["status"] == "dropped"
    assert [c for c in world.calls if c[0] == "mal"] == []
    assert _log(conn, "deferred") == 1
    mal_reconcile.reconcile_mal_progress(conn)       # now MAL's later edit is judged
    assert _status(conn) == "dropped"
    assert world.state["anilist"][100]["status"] == "DROPPED"
    assert world.state["mal"][200]["status"] == "dropped"


def test_a_tie_goes_to_lcars(conn, world):
    _seeded(conn, world, status="watching")
    world.edit("anilist", 100, status="DROPPED")
    stamp = anilist_client._unix_to_iso(world.state["anilist"][100]["t"])
    conn.execute("UPDATE season SET status = 'paused' WHERE id = 'z-hub001'")
    conn.execute(  # LCARS changed at exactly the moment the list did
        "INSERT INTO season_status_change (id, season_id, show_id, previous_status,"
        " new_status, changed_at, changed_by) VALUES ('j-tie001', 'z-hub001', 's-hub001',"
        " 'watching', 'paused', ?, 'user')", (stamp,))
    conn.commit()
    watch_reconcile.reconcile_watch_progress(conn)
    assert _status(conn) == "paused"
    assert world.state["anilist"][100]["status"] == "PAUSED"  # LCARS goes out
    assert _log(conn, "lcars_wins") >= 1


# ── the settle lock ──────────────────────────────────────────────────────


def test_a_row_stays_locked_until_every_list_holds_the_decision(conn, world):
    _seeded(conn, world)
    world.down["mal"] = True
    world.edit("anilist", 100, status="PAUSED")
    watch_reconcile.reconcile_watch_progress(conn)
    assert _status(conn) == "paused"
    assert list_hub.locked(conn, "z-hub001")  # MAL couldn't be written
    world.edit("anilist", 100, status="DROPPED")  # a further outside edit meanwhile
    watch_reconcile.reconcile_watch_progress(conn)
    assert _status(conn) == "paused"  # not taken while the row settles
    assert _log(conn, "deferred") >= 1
    world.down["mal"] = False
    mal_reconcile.reconcile_mal_progress(conn)  # LCARS's decision reaches MAL
    assert not list_hub.locked(conn, "z-hub001")
    watch_reconcile.reconcile_watch_progress(conn)  # now the deferred edit is judged
    assert _status(conn) == "dropped"


def test_a_row_locked_past_the_limit_opens_a_review(conn, world):
    _seeded(conn, world)
    world.down["mal"] = True
    world.edit("anilist", 100, status="PAUSED")
    watch_reconcile.reconcile_watch_progress(conn)
    conn.execute("UPDATE list_row_lock SET since = '2020-01-01T00:00:00Z'")
    list_hub.settle_locked(conn)
    review = conn.execute(
        "SELECT * FROM pending_review WHERE field = 'list_not_settled'").fetchone()
    assert review is not None
    reviews.resolve_choice(conn, review["id"], "unlock", "holodeck", None)
    assert not list_hub.locked(conn, "z-hub001")


# ── progress moving down ─────────────────────────────────────────────────


def test_a_lower_progress_of_up_to_two_episodes_is_applied(conn, world):
    _seeded(conn, world, watched=3, progress=3)
    world.edit("anilist", 100, progress=2)
    watch_reconcile.reconcile_watch_progress(conn)
    assert _watched(conn) == 2
    assert world.state["mal"][200]["progress"] == 2  # and on to MAL


def test_a_lower_progress_of_more_than_two_is_a_review(conn, world):
    _seeded(conn, world, watched=4, progress=4)
    world.edit("anilist", 100, progress=1)
    watch_reconcile.reconcile_watch_progress(conn)
    assert _watched(conn) == 4  # nothing taken
    review = conn.execute(
        "SELECT * FROM pending_review WHERE field = 'remote_progress_lower'").fetchone()
    assert review is not None
    reviews.resolve_choice(conn, review["id"], "accept_lower", "holodeck", None)
    assert _watched(conn) == 1


def test_a_lower_progress_on_a_completed_season_is_a_review_even_for_one_episode(
        conn, world):
    _seeded(conn, world, status="completed", al="COMPLETED", mal="completed",
            watched=5, progress=5)
    world.edit("anilist", 100, progress=4)
    watch_reconcile.reconcile_watch_progress(conn)
    assert _watched(conn) == 5 and _status(conn) == "completed"
    assert conn.execute(
        "SELECT COUNT(*) FROM pending_review WHERE field = 'remote_progress_lower'"
    ).fetchone()[0] == 1


# ── an entry deleted on a list ────────────────────────────────────────────


def test_an_entry_removed_from_a_list_is_a_review_not_a_silent_re_add(conn, world):
    _seeded(conn, world)
    del world.state["mal"][200]
    _poll(conn)
    assert [c for c in world.calls if c[0] == "mal"] == []  # not re-added
    review = conn.execute(
        "SELECT * FROM pending_review WHERE field = 'list_entry_removed'").fetchone()
    assert review is not None
    reviews.resolve_choice(conn, review["id"], "re_add", "holodeck", None)
    assert 200 in world.state["mal"]


def test_stopping_the_mirror_after_a_removal_leaves_the_season_alone(conn, world):
    _seeded(conn, world)
    del world.state["mal"][200]
    _poll(conn)
    review = conn.execute(
        "SELECT * FROM pending_review WHERE field = 'list_entry_removed'").fetchone()
    reviews.resolve_choice(conn, review["id"], "stop_mirroring", "holodeck", None)
    assert conn.execute("SELECT list_sync FROM season WHERE id = 'z-hub001'").fetchone()[0] == 0
    assert list_baseline.get(conn, "mal", 200) is None


# ── until the setup is live ──────────────────────────────────────────────


def test_with_intake_off_nothing_an_outside_list_holds_is_taken(conn, world):
    _seeded(conn, world)
    config.set_current(config.Config(
        anilist_access_token="a", mal_access_token="m", list_intake_enabled=False))
    world.edit("anilist", 100, status="DROPPED", progress=3)
    _poll(conn)
    assert _status(conn) == "watching" and _watched(conn) == 0
    assert world.calls == []
    assert list_baseline.get(conn, "anilist", 100)["status"] == "watching"  # baseline untouched


def test_with_intake_off_lcars_still_retries_its_own_write(conn, world):
    _seeded(conn, world)
    config.set_current(config.Config(
        anilist_access_token="a", mal_access_token="m", list_intake_enabled=False))
    conn.execute("UPDATE season SET status = 'paused' WHERE id = 'z-hub001'")
    conn.commit()
    _poll(conn)
    assert world.state["anilist"][100]["status"] == "PAUSED"


# ── a dry run (writes only captured) ─────────────────────────────────────


def test_a_dry_run_takes_the_change_but_locks_and_records_nothing(conn, world, monkeypatch):
    _seeded(conn, world)
    monkeypatch.setenv("LCARS_EXTERNAL_WRITES", "capture")  # writes only captured
    world.edit("mal", 200, status="on_hold")
    _poll(conn)
    assert _status(conn) == "paused"  # LCARS took it (a copy is safe to change)
    assert conn.execute("SELECT COUNT(*) FROM list_row_lock").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM list_sync_log").fetchone()[0] == 0



# ── R2.15a: the entry's total is stored and can confirm a fully watched level ─────


def test_a_new_episode_total_completes_a_fully_watched_level(conn, world):
    _seeded(conn, world, watched=5, progress=5)  # every known episode watched, total unknown
    assert _status(conn) == "watching"
    world.state["anilist"][100]["episodes"] = 5
    _poll(conn)
    assert conn.execute("SELECT episode_total FROM season WHERE id = 'z-hub001'"
                        ).fetchone()[0] == 5
    assert _status(conn) == "completed"


def test_a_total_that_does_not_match_leaves_it_watching(conn, world):
    _seeded(conn, world, watched=5, progress=5)
    world.state["anilist"][100]["episodes"] = 12
    _poll(conn)
    assert _status(conn) == "watching"


# ── one list entry covering several TVDB seasons (Urusei Yatsura, user 2026-10-05) ──────────


def _shared_show(conn, watched=0, status="watching"):
    """Three TVDB seasons (2 + 2 + 1 episodes, absolute 1–5) that are ONE AniList entry 100 (MAL
    200) — like Urusei Yatsura's 54 + 52 + 43 + 46 = 195."""
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-shr001', 'episodic', 'anime', 'Shared', 'romaji', 'watching', 1, 'x', 'x')")
    n = 0
    for k, (zid, count) in enumerate((("z-shr001", 2), ("z-shr002", 2), ("z-shr003", 1)), 1):
        conn.execute(
            "INSERT INTO season (id, show_id, season_number, status, anilist_id, mal_id, source,"
            " created_at, updated_at) VALUES (?, 's-shr001', ?, ?, 100, 200, 'manual', 'x', 'x')",
            (zid, k, status))
        for service, ext in (("anilist", 100), ("mal", 200)):
            conn.execute(
                "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                " VALUES (?, ?, ?, 'x')", (zid, service, ext))
        conn.execute("INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                     (zid, n + 1, n + count))
        for e in range(1, count + 1):
            n += 1
            conn.execute(
                "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number, state,"
                " air_date_utc, season_id, created_at, updated_at)"
                " VALUES (?, 's-shr001', ?, ?, 'regular', ?, ?, ?, ?, 'x', 'x')",
                (f"e-shr00{n}", k, e, n, "watched" if n <= watched else "unwatched", PAST, zid))
    conn.commit()


def _shared_watched(conn):
    return [r[0] for r in conn.execute(
        "SELECT absolute_number FROM episode WHERE show_id = 's-shr001' AND state = 'watched'"
        " ORDER BY absolute_number")]


def test_one_entry_over_several_seasons_is_not_a_conflict_and_progress_is_by_episode(conn, world):
    _shared_show(conn)
    world.put("anilist", 100, "CURRENT", 0)
    world.put("mal", 200, "watching", 0)
    _poll(conn)  # seed
    world.edit("anilist", 100, progress=3)  # watched three episodes: all of S1, the first of S2
    _poll(conn)
    assert _shared_watched(conn) == [1, 2, 3]
    assert conn.execute("SELECT COUNT(*) FROM pending_review WHERE field LIKE '%id_conflict'"
                        ).fetchone()[0] == 0


def test_lcars_writes_one_combined_progress_and_never_a_status_for_a_shared_entry(conn, world):
    from lcars import list_sync

    _shared_show(conn)
    world.put("anilist", 100, "CURRENT", 0)
    world.put("mal", 200, "watching", 0)
    _poll(conn)
    world.calls.clear()
    conn.execute("UPDATE episode SET state = 'watched' WHERE absolute_number <= 4")
    conn.commit()
    list_sync.push_progress_for_show(conn, "s-shr001")
    anilist = [c for c in world.calls if c[0] == "anilist"]
    assert [(c[1], c[2]) for c in anilist] == [(100, {"progress": 4})]  # once, combined, no status
    mal = [c for c in world.calls if c[0] == "mal"]
    assert [(c[1], c[2]) for c in mal] == [(200, {"num_watched_episodes": 4})]


def test_the_lists_status_is_mirrored_onto_the_last_level_of_a_shared_entry(conn, world):
    _shared_show(conn, watched=2)
    world.put("anilist", 100, "CURRENT", 2)
    world.put("mal", 200, "watching", 2)
    _poll(conn)
    world.edit("anilist", 100, status="PAUSED")
    _poll(conn)
    statuses = dict(conn.execute("SELECT id, status FROM season WHERE show_id = 's-shr001'"))
    assert statuses["z-shr003"] == "paused"  # the last level speaks for the entry
    assert statuses["z-shr001"] == "watching" and statuses["z-shr002"] == "watching"


def test_a_shared_entry_is_one_group_in_list_sync(conn):
    from lcars import list_sync

    _shared_show(conn)
    last = conn.execute("SELECT * FROM season WHERE id = 'z-shr003'").fetchone()
    first = conn.execute("SELECT * FROM season WHERE id = 'z-shr001'").fetchone()
    assert [lv["id"] for lv in list_sync.group_levels(conn, first)] == [
        "z-shr001", "z-shr002", "z-shr003"]
    assert list_sync.is_group_representative(conn, last)
    assert not list_sync.is_group_representative(conn, first)
    assert len(list_sync.level_episodes_ordered(conn, first)) == 5


def test_two_different_shows_sharing_an_id_are_still_a_conflict(conn, world):
    _shared_show(conn)
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-shr002', 'episodic', 'anime', 'Other', 'romaji', 'watching', 1, 'x', 'x')")
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id, source,"
        " created_at, updated_at) VALUES ('z-oth001', 's-shr002', 1, 'watching', 100, 'manual',"
        " 'x', 'x')")
    conn.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                 " VALUES ('z-oth001', 'anilist', 100, 'x')")
    conn.commit()
    world.put("anilist", 100, "CURRENT", 0)
    _poll(conn)
    assert conn.execute("SELECT COUNT(*) FROM pending_review WHERE field = 'anilist_id_conflict'"
                        ).fetchone()[0] >= 1


def test_the_old_conflict_reviews_of_a_shared_entry_are_closed(conn, world):
    _shared_show(conn)
    for i, zid in enumerate(("z-shr001", "z-shr002", "z-shr003")):
        conn.execute(
            "INSERT INTO pending_review (id, entity_type, entity_id, field, proposed_value_chain,"
            " source, created_at) VALUES (?, 'season', ?, 'anilist_id_conflict', '[]',"
            " 'anilist_reconcile', 'x')", (f"r-old00{i}", zid))
    conn.commit()
    world.put("anilist", 100, "CURRENT", 0)
    _poll(conn)
    open_ = conn.execute("SELECT COUNT(*) FROM pending_review WHERE field = 'anilist_id_conflict'"
                         " AND resolved_at IS NULL").fetchone()[0]
    assert open_ == 0
