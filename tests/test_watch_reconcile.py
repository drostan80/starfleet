"""B.15 — AniList -> LCARS watch reconciliation, live-caught 2026-08-12.
Same real-migrated-SQLite-DB approach test_show_merge.py already
established for exactly the same reason: this module's write pattern
(bulk watch_event backfill + episode.state + show.status correction)
is real enough that a mocked connection would hide the exact bugs it
exists to avoid.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import anilist_client, config, watch_reconcile


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "watch_reconcile_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture(autouse=True)
def _reset_config():
    config.set_current(config.Config())
    yield
    config.set_current(config.Config())


def _show(conn, show_id, title="Show", status="planned", tracked=1):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'romaji', ?, ?, 'x', 'x')",
        (show_id, title, status, tracked),
    )


def _season(conn, season_id, show_id, season_number, anilist_id):
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number, anilist_id),
    )
    # S3: _apply_remote_list reads from season_external_id, not season.anilist_id —
    # mirror into the table so test fixtures are found by the reconciler.
    if anilist_id is not None:
        conn.execute(
            "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
            " VALUES (?, 'anilist', ?, 'x')",
            (season_id, anilist_id),
        )


def _episode(conn, episode_id, show_id, season, episode, state="unwatched"):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, 'x', 'x')",
        (episode_id, show_id, season, episode, state),
    )


def _watch_event(conn, event_id, show_id, season, episode):
    conn.execute(
        "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
        " VALUES (?, ?, ?, ?, 'x', 'x')",
        (event_id, show_id, season, episode),
    )


def _configure_anilist(monkeypatch, entries: list[dict]):
    config.set_current(config.Config(anilist_access_token="tok"))
    monkeypatch.setattr(anilist_client, "fetch_my_anime_list", lambda token: entries)


def _entry(anilist_id, status="CURRENT", progress=0):
    return {"anilist_id": anilist_id, "status": status, "progress": progress, "format": "TV"}


def test_returns_all_zero_when_anilist_not_configured(conn):
    result = watch_reconcile.reconcile_watch_progress(conn)
    assert result == {
        "seasons_checked": 0,
        "not_matched_on_anilist": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_anilist_id_conflicts": 0,
    }


def test_backfills_unwatched_episodes_up_to_anilist_progress(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _episode(conn, "e-episd1", "s-showw1", 1, 1)
    _episode(conn, "e-episd2", "s-showw1", 1, 2)
    _episode(conn, "e-episd3", "s-showw1", 1, 3)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=3)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["episodes_backfilled"] == 3
    assert result["seasons_checked"] == 1
    states = conn.execute(
        "SELECT episode, state FROM episode WHERE show_id = 's-showw1'"
    ).fetchall()
    assert all(row["state"] == "watched" for row in states)
    events = conn.execute("SELECT episode FROM watch_event WHERE show_id = 's-showw1'").fetchall()
    assert sorted(r["episode"] for r in events) == [1, 2, 3]


def test_never_touches_a_skipped_episode(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _episode(conn, "e-episd1", "s-showw1", 1, 1, state="unwatched")
    _episode(conn, "e-episd2", "s-showw1", 1, 2, state="skipped")
    _episode(conn, "e-episd3", "s-showw1", 1, 3, state="unwatched")
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=3)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    # Only the two genuinely-unwatched episodes backfilled — the deliberate skip is untouched.
    assert result["episodes_backfilled"] == 2
    ep2 = conn.execute("SELECT state FROM episode WHERE id = 'e-episd2'").fetchone()
    assert ep2["state"] == "skipped"
    assert conn.execute("SELECT * FROM watch_event WHERE episode = 2").fetchone() is None


def test_does_not_duplicate_an_already_watched_episode(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _episode(conn, "e-episd1", "s-showw1", 1, 1, state="watched")
    _watch_event(conn, "w-exist1", "s-showw1", 1, 1)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["episodes_backfilled"] == 0
    events = conn.execute("SELECT id FROM watch_event WHERE show_id = 's-showw1'").fetchall()
    assert len(events) == 1  # still just the original, no duplicate


def test_updates_show_status_when_it_diverges_from_anilist(conn, monkeypatch):
    _show(conn, "s-showw1", status="completed")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=0)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["shows_status_updated"] == 1
    row = conn.execute("SELECT status FROM show WHERE id = 's-showw1'").fetchone()
    assert row["status"] == "watching"
    change = conn.execute(
        "SELECT previous_status, new_status, changed_by FROM status_change"
        " WHERE show_id = 's-showw1'"
    ).fetchone()
    assert change["previous_status"] == "completed"
    assert change["new_status"] == "watching"
    assert change["changed_by"] == "anilist_reconcile"


def test_leaves_status_unchanged_and_writes_no_history_when_already_correct(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=0)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["shows_status_updated"] == 0
    assert conn.execute("SELECT * FROM status_change WHERE show_id = 's-showw1'").fetchone() is None


def test_multi_season_show_uses_the_highest_season_numbers_status(conn, monkeypatch):
    _show(conn, "s-showw1", status="completed")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    _season(conn, "z-seasn2", "s-showw1", 2, anilist_id=200)
    conn.commit()
    # Season 1 finished (COMPLETED); season 2 (the current cour) is CURRENT —
    # the show-level status should follow season 2, not season 1.
    _configure_anilist(
        monkeypatch,
        [_entry(100, status="COMPLETED", progress=12), _entry(200, status="CURRENT", progress=2)],
    )

    watch_reconcile.reconcile_watch_progress(conn)

    row = conn.execute("SELECT status FROM show WHERE id = 's-showw1'").fetchone()
    assert row["status"] == "watching"


def test_maps_repeating_to_watching(conn, monkeypatch):
    _show(conn, "s-showw1", status="completed")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="REPEATING", progress=1)])

    watch_reconcile.reconcile_watch_progress(conn)

    row = conn.execute("SELECT status FROM show WHERE id = 's-showw1'").fetchone()
    assert row["status"] == "watching"


def test_counts_a_season_with_no_matching_anilist_entry(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    _season(conn, "z-seasn1", "s-showw1", 1, anilist_id=999)  # not on the fetched list below
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["not_matched_on_anilist"] == 1
    assert result["seasons_checked"] == 0


def test_ignores_seasons_with_no_anilist_id_at_all(conn, monkeypatch):
    _show(conn, "s-showw1", status="watching")
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, source, created_at, updated_at)"
        " VALUES ('z-seasn1', 's-showw1', 1, 'manual', 'x', 'x')"
    )
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result == {
        "seasons_checked": 0,
        "not_matched_on_anilist": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_anilist_id_conflicts": 0,
    }


# --- poll_anilist_activity (B.5.3) --------------------------------------


def _configure_anilist_activity(monkeypatch, *, marker=None, feed=None, reconcile_entries=None):
    config.set_current(config.Config(anilist_access_token="tok"))
    monkeypatch.setattr(anilist_client, "fetch_viewer_id", lambda token: 24011)
    monkeypatch.setattr(anilist_client, "fetch_latest_activity_marker", lambda token, uid: marker)
    monkeypatch.setattr(
        anilist_client,
        "fetch_activity_feed",
        lambda token, uid, since_id, since_created_at: feed or [],
    )
    monkeypatch.setattr(
        anilist_client, "fetch_my_anime_list", lambda token: reconcile_entries or []
    )


def test_poll_anilist_activity_returns_zero_when_anilist_not_configured(conn):
    result = watch_reconcile.poll_anilist_activity(conn)
    assert result == {"activities_seen": 0, "reconcile_result": None}


def test_poll_anilist_activity_first_call_seeds_checkpoint_without_reconciling(conn, monkeypatch):
    """The real reason this exists — confirmed live 2026-08-13 that a
    real account can carry 5000+ activities: a first-ever call must
    never walk that whole history or trigger a reconcile over it."""
    called_feed = []
    _configure_anilist_activity(monkeypatch, marker=(999, 1700000000))
    monkeypatch.setattr(
        anilist_client,
        "fetch_activity_feed",
        lambda *a, **kw: called_feed.append(1) or [],
    )

    result = watch_reconcile.poll_anilist_activity(conn)

    assert result == {"activities_seen": 0, "reconcile_result": None}
    assert called_feed == []  # never even called on the seeding pass
    row = conn.execute(
        "SELECT last_activity_id, last_activity_created_at"
        " FROM anilist_activity_checkpoint WHERE id = 1"
    ).fetchone()
    assert row["last_activity_id"] == 999
    assert row["last_activity_created_at"] == 1700000000


def test_poll_anilist_activity_first_call_with_no_prior_activity_seeds_zero(conn, monkeypatch):
    _configure_anilist_activity(monkeypatch, marker=None)
    result = watch_reconcile.poll_anilist_activity(conn)
    assert result == {"activities_seen": 0, "reconcile_result": None}
    row = conn.execute(
        "SELECT last_activity_id, last_activity_created_at"
        " FROM anilist_activity_checkpoint WHERE id = 1"
    ).fetchone()
    assert row["last_activity_id"] == 0
    assert row["last_activity_created_at"] == 0


def test_poll_anilist_activity_no_new_activity_does_not_reconcile(conn, monkeypatch):
    conn.execute(
        "INSERT INTO anilist_activity_checkpoint"
        " (id, last_activity_id, last_activity_created_at, updated_at)"
        " VALUES (1, 500, 1700000000, 'x')"
    )
    conn.commit()
    _configure_anilist_activity(monkeypatch, feed=[])

    result = watch_reconcile.poll_anilist_activity(conn)

    assert result == {"activities_seen": 0, "reconcile_result": None}
    row = conn.execute(
        "SELECT last_activity_id FROM anilist_activity_checkpoint WHERE id = 1"
    ).fetchone()
    assert row["last_activity_id"] == 500  # untouched — nothing to advance to


def test_poll_anilist_activity_new_activity_triggers_reconcile_and_advances_checkpoint(
    conn, monkeypatch
):
    _show(conn, "s-pol001", status="planned")
    _season(conn, "z-pol001", "s-pol001", season_number=1, anilist_id=100)
    conn.execute(
        "INSERT INTO anilist_activity_checkpoint"
        " (id, last_activity_id, last_activity_created_at, updated_at)"
        " VALUES (1, 500, 1700000000, 'x')"
    )
    conn.commit()
    feed = [
        {"id": 501, "created_at": 1700000100},
        {"id": 502, "created_at": 1700000200},
    ]
    _configure_anilist_activity(
        monkeypatch, feed=feed, reconcile_entries=[_entry(100, status="CURRENT", progress=0)]
    )

    result = watch_reconcile.poll_anilist_activity(conn)

    assert result["activities_seen"] == 2
    assert result["reconcile_result"] is not None
    assert result["reconcile_result"]["shows_status_updated"] == 1  # planned -> watching, applied

    row = conn.execute("SELECT status FROM show WHERE id = 's-pol001'").fetchone()
    assert row["status"] == "watching"

    checkpoint = conn.execute(
        "SELECT last_activity_id, last_activity_created_at"
        " FROM anilist_activity_checkpoint WHERE id = 1"
    ).fetchone()
    assert checkpoint["last_activity_id"] == 502  # advanced to the newest seen
    assert checkpoint["last_activity_created_at"] == 1700000200


# --- reconcile hardening, 2026-08-13: real bugs found + fixed after -----
# the first live triggered reconcile via B.5.3, both reproduced directly
# before being fixed, not theoretical.


def test_two_seasons_sharing_one_anilist_id_are_excluded_not_cross_applied(conn, monkeypatch):
    """The real, reproduced bug: nothing anywhere in this codebase
    checks for this (setSeasonMapping has no such guard; B.14's own
    duplicate detection watches a different signal and never sees a
    pair that already both have an anilist link) — and unlike an
    unmatched season, it doesn't self-heal. Both seasons must be
    excluded from this run entirely, not just have one side "win"."""
    _show(conn, "s-dup001", status="planned")
    _show(conn, "s-dup002", status="planned")
    _season(conn, "z-dup001", "s-dup001", 1, anilist_id=999)
    _season(conn, "z-dup002", "s-dup002", 1, anilist_id=999)
    _episode(conn, "e-dup001", "s-dup001", 1, 1)
    _episode(conn, "e-dup002", "s-dup002", 1, 1)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(999, status="COMPLETED", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["ambiguous_anilist_id_conflicts"] == 2
    assert result["seasons_checked"] == 0  # neither side counted as a real match this run
    assert result["shows_status_updated"] == 0
    assert result["episodes_backfilled"] == 0

    for show_id in ("s-dup001", "s-dup002"):
        row = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        assert row["status"] == "planned"  # untouched, not cross-contaminated
        ep = conn.execute("SELECT state FROM episode WHERE show_id = ?", (show_id,)).fetchone()
        assert ep["state"] == "unwatched"


def test_two_seasons_sharing_one_anilist_id_each_get_their_own_pending_review(conn, monkeypatch):
    _show(conn, "s-dup001", status="planned")
    _show(conn, "s-dup002", status="planned")
    _season(conn, "z-dup001", "s-dup001", 1, anilist_id=999)
    _season(conn, "z-dup002", "s-dup002", 1, anilist_id=999)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(999, status="COMPLETED", progress=0)])

    watch_reconcile.reconcile_watch_progress(conn)

    reviews = conn.execute(
        "SELECT entity_id, field FROM pending_review WHERE field = 'anilist_id_conflict'"
    ).fetchall()
    assert {r["entity_id"] for r in reviews} == {"z-dup001", "z-dup002"}


def test_subdivision_same_tvdb_suppresses_conflict(conn, monkeypatch):
    """Shows that share a TVDB ID are season subdivisions of the same
    Sonarr series.  A shared AniList ID between them is expected — the
    reconciler should NOT flag it as a conflict, and should still apply
    the remote watch state to both seasons normally."""
    _show(conn, "s-sub001", status="planned")
    _show(conn, "s-sub002", status="planned")
    # Both shows share TVDB 355774 (e.g. Dr. STONE parent + split-out cour).
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-sub001', 'tvdb', '355774', '', 'x')"
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-sub002', 'tvdb', '355774', '', 'x')"
    )
    _season(conn, "z-sub001", "s-sub001", 3, anilist_id=162670)
    _season(conn, "z-sub002", "s-sub002", 1, anilist_id=162670)
    _episode(conn, "e-sub001", "s-sub001", 3, 1)
    _episode(conn, "e-sub002", "s-sub002", 1, 1)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(162670, status="COMPLETED", progress=1)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    # No conflict flagged — subdivision suppression.
    assert result["ambiguous_anilist_id_conflicts"] == 0
    # No pending_review created.
    reviews = conn.execute(
        "SELECT * FROM pending_review WHERE field = 'anilist_id_conflict'"
    ).fetchall()
    assert len(reviews) == 0
    # Watch state was still applied to both seasons' episodes.
    for eid in ("e-sub001", "e-sub002"):
        ep = conn.execute("SELECT state FROM episode WHERE id = ?", (eid,)).fetchone()
        assert ep["state"] == "watched"


def test_a_stale_link_on_the_highest_season_does_not_fall_back_to_a_lower_seasons_status(
    conn, monkeypatch
):
    """The real, reproduced bug: a show genuinely `watching` (its real
    current season not yet matched on the real list — e.g. linked but
    not yet added there) got wrongly reverted to `completed` by an
    older, unrelated, already-finished season. This case is expected
    to self-heal once the real match appears — deliberately NOT a
    pending_review, unlike the conflict case above."""
    _show(conn, "s-stl001", status="watching")  # user IS watching the new season right now
    _season(conn, "z-stl001", "s-stl001", 1, anilist_id=100)  # old, finished cour — resolves fine
    _season(conn, "z-stl002", "s-stl001", 2, anilist_id=999999)  # current cour — never resolves
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="COMPLETED", progress=0)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["shows_status_updated"] == 0  # refused to guess from the lower season
    assert result["not_matched_on_anilist"] == 1  # season 2 correctly counted as unmatched
    row = conn.execute("SELECT status FROM show WHERE id = 's-stl001'").fetchone()
    assert row["status"] == "watching"  # untouched, not wrongly reverted
    # No pending_review opened for this case — expected to self-heal.
    assert (
        conn.execute("SELECT * FROM pending_review WHERE entity_id = 'z-stl002'").fetchone() is None
    )


def test_the_true_highest_season_status_still_applies_normally_when_it_resolves(conn, monkeypatch):
    """Confirms the fix didn't just break the normal multi-season case
    — when the highest season DOES resolve, its status still wins,
    same as before this hardening pass."""
    _show(conn, "s-stl002", status="completed")
    _season(conn, "z-stl003", "s-stl002", 1, anilist_id=100)
    _season(conn, "z-stl004", "s-stl002", 2, anilist_id=200)
    conn.commit()
    _configure_anilist(
        monkeypatch,
        [_entry(100, status="COMPLETED", progress=0), _entry(200, status="CURRENT", progress=0)],
    )

    result = watch_reconcile.reconcile_watch_progress(conn)

    assert result["shows_status_updated"] == 1
    row = conn.execute("SELECT status FROM show WHERE id = 's-stl002'").fetchone()
    assert row["status"] == "watching"  # season 2's status, not season 1's


# --- season-level write + show-status derivation guard (2026-09-20) -----------


def test_reconcile_writes_the_seasons_own_status_not_just_shows(conn, monkeypatch):
    """2026-09-20 fix: _apply_remote_list used to write show.status
    directly and never touch season.status at all — the only way a
    season's status ever changed was the manual setSeasonStatus mutation.
    Remote reconcile should mirror the remote at the season level (exactly
    what setSeasonStatus already does by hand) and let the existing,
    single-authority _recompute_show_status derive the show level from
    that, the same as every other status-writing path in the codebase."""
    _show(conn, "s-wrt001", status="planned")
    _season(conn, "z-wrt001", "s-wrt001", 1, anilist_id=100)
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="CURRENT", progress=0)])

    watch_reconcile.reconcile_watch_progress(conn)

    season = conn.execute("SELECT status FROM season WHERE id = 'z-wrt001'").fetchone()
    assert season["status"] == "watching"
    show = conn.execute("SELECT status FROM show WHERE id = 's-wrt001'").fetchone()
    assert show["status"] == "watching"


def test_remote_completed_season_never_promotes_show_status_over_a_real_gap(conn, monkeypatch):
    """The live bug, reproduced: AniList reports the highest season as
    COMPLETED, but LCARS has a real, already-aired, unwatched episode for
    that exact season (both platforms can flip a currently-airing entry's
    own status as their episode-count metadata catches up — this isn't
    hypothetical, it reproduced for two real shows in production, flipping
    status.status back and forth for over a month). The show must not
    follow the remote into 'completed', and must not fabricate a
    watch_event via the resulting auto-complete bulk-mark
    (_bulk_mark_all_aired_episodes_watched). The season's own status still
    mirrors the remote faithfully — only the show-level derivation, which
    every other real caller in the codebase already trusts, is protected."""
    _show(conn, "s-gap001", status="watching")
    _season(conn, "z-gap001", "s-gap001", 1, anilist_id=100)
    conn.execute(
        "INSERT INTO episode"
        " (id, show_id, season, episode, kind, state, air_date_utc, created_at, updated_at)"
        " VALUES ('e-gap001', 's-gap001', 1, 1, 'regular', 'unwatched',"
        " '2020-01-01T00:00:00Z', 'x', 'x')"
    )
    conn.commit()
    _configure_anilist(monkeypatch, [_entry(100, status="COMPLETED", progress=0)])

    result = watch_reconcile.reconcile_watch_progress(conn)

    # Season status mirrors the remote — legitimate, matches setSeasonStatus.
    season = conn.execute("SELECT status FROM season WHERE id = 'z-gap001'").fetchone()
    assert season["status"] == "completed"
    # Show status must NOT follow it — a real gap exists.
    show = conn.execute("SELECT status FROM show WHERE id = 's-gap001'").fetchone()
    assert show["status"] == "watching"
    assert result["shows_status_updated"] == 0
    # No fabricated watch event for the still-unwatched episode.
    assert (
        conn.execute("SELECT * FROM watch_event WHERE show_id = 's-gap001'").fetchone() is None
    )
    ep = conn.execute("SELECT state FROM episode WHERE id = 'e-gap001'").fetchone()
    assert ep["state"] == "unwatched"


# --- onward push to MAL (2026-08-26, bidirectional hub) -----------------------


def test_anilist_reconcile_pushes_changes_onward_to_mal(conn, monkeypatch):
    from lcars import mal_client

    _show(conn, "s-hubma1", status="planned")
    _season(conn, "z-hubma1", "s-hubma1", 1, anilist_id=100)
    conn.execute("UPDATE season SET mal_id = 555 WHERE id = 'z-hubma1'")
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES ('z-hubma1', 'mal', 555, 'x')"
    )
    _episode(conn, "e-hubp11", "s-hubma1", 1, 1)
    _episode(conn, "e-hubp12", "s-hubma1", 1, 2)
    conn.commit()

    # AniList says watching, progress 2 -> LCARS changes -> must mirror to MAL.
    config.set_current(config.Config(anilist_access_token="atok", mal_access_token="mtok"))
    monkeypatch.setattr(
        anilist_client, "fetch_my_anime_list", lambda token: [_entry(100, "CURRENT", progress=2)]
    )
    mal_calls = []
    monkeypatch.setattr(
        mal_client,
        "update_my_list_status",
        lambda token, mal_id, **kw: mal_calls.append({"mal_id": mal_id, **kw}),
    )

    watch_reconcile.reconcile_watch_progress(conn)

    # status mirrored (watching) and progress mirrored (2) to MAL id 555
    assert {"mal_id": 555, "status": "watching"} in mal_calls
    assert {"mal_id": 555, "num_watched_episodes": 2} in mal_calls


def test_anilist_reconcile_no_change_pushes_nothing_to_mal(conn, monkeypatch):
    from lcars import mal_client

    _show(conn, "s-hubma2", status="watching")
    _season(conn, "z-hubma2", "s-hubma2", 1, anilist_id=101)
    conn.execute("UPDATE season SET mal_id = 556 WHERE id = 'z-hubma2'")
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES ('z-hubma2', 'mal', 556, 'x')"
    )
    _episode(conn, "e-hubp21", "s-hubma2", 1, 1, state="watched")
    conn.commit()

    config.set_current(config.Config(anilist_access_token="atok", mal_access_token="mtok"))
    monkeypatch.setattr(
        anilist_client, "fetch_my_anime_list", lambda token: [_entry(101, "CURRENT", progress=1)]
    )
    mal_calls = []
    monkeypatch.setattr(
        mal_client,
        "update_my_list_status",
        lambda token, mal_id, **kw: mal_calls.append({"mal_id": mal_id, **kw}),
    )

    watch_reconcile.reconcile_watch_progress(conn)

    assert mal_calls == []  # already converged -> no onward push
