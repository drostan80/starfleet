"""Cross-service show-duplicate merge — SCOPE.md's show-merge note,
BUILD_PLAN.md B.14. Same real-migrated-SQLite-DB approach
test_show_backfill.py already established — this module's write
pattern (deferred FK, multi-table reparenting) is real enough that a
fake/mocked connection would hide the exact bugs it exists to avoid.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import show_merge


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "show_merge_test.db"
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


def _show(conn, show_id, title, tracking_space="anime", tracked=1, status="planned"):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, ?, 'romaji', ?, ?, 'x', 'x')",
        (show_id, tracking_space, title, status, tracked),
    )


def _external_id(conn, show_id, service, external_id):
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, 'https://x', 'x')",
        (show_id, service, external_id),
    )


def _episode(conn, episode_id, show_id, season, episode, season_id=None):
    conn.execute(
        "INSERT INTO episode"
        " (id, show_id, season, episode, kind, created_at, updated_at, season_id)"
        " VALUES (?, ?, ?, ?, 'regular', 'x', 'x', ?)",
        (episode_id, show_id, season, episode, season_id),
    )


def _season(conn, season_id, show_id, season_number):
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, source, created_at, updated_at)"
        " VALUES (?, ?, ?, 'manual', 'x', 'x')",
        (season_id, show_id, season_number),
    )


def _watch_event(conn, event_id, show_id, season, episode):
    conn.execute(
        "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
        " VALUES (?, ?, ?, ?, 'x', 'x')",
        (event_id, show_id, season, episode),
    )


# --- find_candidate_pairs ----------------------------------------------------


def test_find_candidate_pairs_matches_by_title(conn):
    _show(conn, "s-loser1", "Mebius Dust", tracking_space="tv")
    _external_id(conn, "s-loser1", "tvdb", "111")
    _show(conn, "s-winnr1", "Mebius Dust", tracking_space="anime")
    _external_id(conn, "s-winnr1", "anilist", "222")
    conn.commit()

    pairs = show_merge.find_candidate_pairs(conn)
    assert len(pairs) == 1
    loser_id, winner_id, matched_on = pairs[0]
    assert loser_id == "s-loser1"
    assert winner_id == "s-winnr1"
    assert "Mebius Dust" in matched_on


def test_find_candidate_pairs_ignores_a_show_with_both_links(conn):
    _show(conn, "s-both01", "Already Linked", tracking_space="anime")
    _external_id(conn, "s-both01", "tvdb", "111")
    _external_id(conn, "s-both01", "anilist", "222")
    conn.commit()
    assert show_merge.find_candidate_pairs(conn) == []


def test_find_candidate_pairs_ignores_untracked_shows(conn):
    _show(conn, "s-loser2", "Untracked Show", tracking_space="tv", tracked=0)
    _external_id(conn, "s-loser2", "tvdb", "111")
    _show(conn, "s-winnr2", "Untracked Show", tracking_space="anime")
    _external_id(conn, "s-winnr2", "anilist", "222")
    conn.commit()
    assert show_merge.find_candidate_pairs(conn) == []


def test_find_candidate_pairs_skips_ambiguous_title_matches(conn):
    _show(conn, "s-loser3", "Duplicate Title", tracking_space="tv")
    _external_id(conn, "s-loser3", "tvdb", "111")
    _show(conn, "s-winnr3", "Duplicate Title", tracking_space="anime")
    _external_id(conn, "s-winnr3", "anilist", "222")
    _show(conn, "s-winnr4", "Duplicate Title", tracking_space="anime")
    _external_id(conn, "s-winnr4", "anilist", "333")
    conn.commit()
    assert show_merge.find_candidate_pairs(conn) == []


def test_find_candidate_pairs_ignores_a_non_anime_winner_candidate(conn):
    _show(conn, "s-loser4", "Cross Space", tracking_space="tv")
    _external_id(conn, "s-loser4", "tvdb", "111")
    _show(conn, "s-winnr5", "Cross Space", tracking_space="tv")
    _external_id(conn, "s-winnr5", "anilist", "222")
    conn.commit()
    assert show_merge.find_candidate_pairs(conn) == []


# --- merge_shows: validation --------------------------------------------------


def test_merge_shows_rejects_self_merge(conn):
    _show(conn, "s-self01", "Self")
    conn.commit()
    with pytest.raises(ValueError, match="itself"):
        show_merge.merge_shows(conn, "s-self01", "s-self01", "test")


def test_merge_shows_rejects_unknown_show(conn):
    _show(conn, "s-real01", "Real")
    conn.commit()
    with pytest.raises(ValueError, match="no such show"):
        show_merge.merge_shows(conn, "s-real01", "s-ghost1", "test")


def test_merge_shows_rejects_an_untracked_loser(conn):
    _show(conn, "s-winnr6", "Winner")
    _show(conn, "s-loser6", "Loser", tracked=0)
    conn.commit()
    with pytest.raises(ValueError, match="tracked"):
        show_merge.merge_shows(conn, "s-winnr6", "s-loser6", "test")


# --- merge_shows: the real operation ------------------------------------------


def test_merge_shows_moves_the_tvdb_link_and_demotes_the_loser(conn):
    _show(conn, "s-winnr7", "Winner")
    _external_id(conn, "s-winnr7", "anilist", "222")
    _show(conn, "s-loser7", "Loser", tracking_space="tv")
    _external_id(conn, "s-loser7", "tvdb", "111")
    conn.commit()

    show_merge.merge_shows(conn, "s-winnr7", "s-loser7", "test")

    winner_services = {
        r["service"]
        for r in conn.execute(
            "SELECT service FROM show_external_id WHERE show_id = 's-winnr7'"
        ).fetchall()
    }
    assert winner_services == {"anilist", "tvdb"}
    loser_services = conn.execute(
        "SELECT service FROM show_external_id WHERE show_id = 's-loser7'"
    ).fetchall()
    assert loser_services == []
    loser_tracked = conn.execute("SELECT tracked FROM show WHERE id = 's-loser7'").fetchone()
    assert loser_tracked["tracked"] == 0


def test_merge_shows_moves_episodes_and_matching_watch_events(conn):
    """The exact scenario advisor flagged as the one to verify: the
    episodes move, and so does the watch_event that references them —
    without tripping the deferred composite FK (show_id, season,
    episode) -> episode (show_id, season, episode)."""
    _show(conn, "s-winnr8", "Winner")
    _show(conn, "s-loser8", "Loser", tracking_space="tv")
    _episode(conn, "e-ep0001", "s-loser8", 1, 1)
    _episode(conn, "e-ep0002", "s-loser8", 1, 2)
    _watch_event(conn, "w-we0001", "s-loser8", 1, 1)
    conn.commit()

    show_merge.merge_shows(conn, "s-winnr8", "s-loser8", "test")

    winner_episodes = {
        (r["season"], r["episode"])
        for r in conn.execute(
            "SELECT season, episode FROM episode WHERE show_id = 's-winnr8'"
        ).fetchall()
    }
    assert winner_episodes == {(1, 1), (1, 2)}
    assert (
        conn.execute("SELECT COUNT(*) c FROM episode WHERE show_id = 's-loser8'").fetchone()["c"]
        == 0
    )
    watch_event = conn.execute("SELECT show_id FROM watch_event WHERE id = 'w-we0001'").fetchone()
    assert watch_event["show_id"] == "s-winnr8"

    # foreign_keys=ON the whole time — no PRAGMA foreign_key_check violations left behind
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_merge_shows_leaves_a_conflicting_episode_on_the_loser(conn):
    _show(conn, "s-winnr9", "Winner")
    _episode(conn, "e-ep0003", "s-winnr9", 1, 1)
    _show(conn, "s-loser9", "Loser", tracking_space="tv")
    _episode(conn, "e-ep0004", "s-loser9", 1, 1)  # same (season, episode) as winner's
    _episode(conn, "e-ep0005", "s-loser9", 1, 2)  # no conflict
    conn.commit()

    merge_id = show_merge.merge_shows(conn, "s-winnr9", "s-loser9", "test")

    # the conflicting episode stayed on the loser
    assert (
        conn.execute("SELECT show_id FROM episode WHERE id = 'e-ep0004'").fetchone()["show_id"]
        == "s-loser9"
    )
    # the non-conflicting one moved
    assert (
        conn.execute("SELECT show_id FROM episode WHERE id = 'e-ep0005'").fetchone()["show_id"]
        == "s-winnr9"
    )
    manifest = conn.execute("SELECT manifest FROM show_merge WHERE id = ?", (merge_id,)).fetchone()
    assert "s1e1" in manifest["manifest"]  # the conflict got logged


def test_merge_shows_repoints_a_conflicting_seasons_episodes_onto_the_winners_season(conn):
    _show(conn, "s-wina10", "Winner")
    _season(conn, "z-seaw10", "s-wina10", 1)
    _show(conn, "s-losa10", "Loser", tracking_space="tv")
    _season(conn, "z-seal10", "s-losa10", 1)  # conflicts with winner's season 1
    _episode(conn, "e-epa010", "s-losa10", 1, 1, season_id="z-seal10")
    conn.commit()

    show_merge.merge_shows(conn, "s-wina10", "s-losa10", "test")

    moved_episode = conn.execute(
        "SELECT show_id, season_id FROM episode WHERE id = 'e-epa010'"
    ).fetchone()
    assert moved_episode["show_id"] == "s-wina10"
    assert moved_episode["season_id"] == "z-seaw10"  # repointed onto the winner's own season row
    # the loser's own season row was left in place, not deleted
    loser_season = conn.execute("SELECT show_id FROM season WHERE id = 'z-seal10'").fetchone()
    assert loser_season["show_id"] == "s-losa10"


def test_merge_shows_moves_a_non_conflicting_season_wholesale(conn):
    _show(conn, "s-wina11", "Winner")
    _show(conn, "s-losa11", "Loser", tracking_space="tv")
    _season(conn, "z-seal11", "s-losa11", 1)
    _episode(conn, "e-epa011", "s-losa11", 1, 1, season_id="z-seal11")
    conn.commit()

    show_merge.merge_shows(conn, "s-wina11", "s-losa11", "test")

    moved_season = conn.execute("SELECT show_id FROM season WHERE id = 'z-seal11'").fetchone()
    assert moved_season["show_id"] == "s-wina11"
    moved_episode = conn.execute(
        "SELECT show_id, season_id FROM episode WHERE id = 'e-epa011'"
    ).fetchone()
    assert moved_episode["season_id"] == "z-seal11"  # unchanged — the season row itself moved


def test_merge_shows_never_deletes_the_loser_show_row(conn):
    _show(conn, "s-wina12", "Winner")
    _show(conn, "s-losa12", "Loser", tracking_space="tv")
    conn.commit()
    show_merge.merge_shows(conn, "s-wina12", "s-losa12", "test")
    assert conn.execute("SELECT id FROM show WHERE id = 's-losa12'").fetchone() is not None


# --- sweep_show_merges (discovery only since 2026-08-12) -----------------------


def test_sweep_show_merges_opens_a_review_for_every_real_candidate(conn):
    _show(conn, "s-swpl01", "Sweep Show", tracking_space="tv")
    _external_id(conn, "s-swpl01", "tvdb", "111")
    _show(conn, "s-swpw01", "Sweep Show", tracking_space="anime")
    _external_id(conn, "s-swpw01", "anilist", "222")
    conn.commit()

    result = show_merge.sweep_show_merges(conn)
    assert result == {"candidates_found": 1, "reviews_opened": 1}
    # Never merges — the loser is untouched, still tracked, nothing moved.
    assert conn.execute("SELECT tracked FROM show WHERE id = 's-swpl01'").fetchone()["tracked"] == 1
    review = conn.execute(
        "SELECT entity_type, entity_id, field, proposed_value_chain FROM pending_review"
        " WHERE entity_id = 's-swpl01'"
    ).fetchone()
    assert review["entity_type"] == "show"
    assert review["field"] == "cross_service_merge"
    assert "s-swpw01" in review["proposed_value_chain"]


def test_sweep_show_merges_opens_a_review_for_each_loser_sharing_a_winner(conn):
    """Two losers can both fuzzy-match the same winner (plausible with
    sequels) — before 2026-08-12 this risked orphaning data if both got
    auto-merged; now it's harmless, since nothing is applied
    automatically — both simply get their own review entry."""
    _show(conn, "s-swpw02", "Shared Winner", tracking_space="anime")
    _external_id(conn, "s-swpw02", "anilist", "999")
    _show(conn, "s-swpl02", "Shared Winner", tracking_space="tv")
    _external_id(conn, "s-swpl02", "tvdb", "111")
    _show(conn, "s-swpl03", "Shared Winner", tracking_space="tv")
    _external_id(conn, "s-swpl03", "tvdb", "222")
    conn.commit()

    result = show_merge.sweep_show_merges(conn)
    assert result == {"candidates_found": 2, "reviews_opened": 2}
    reviews = conn.execute(
        "SELECT entity_id FROM pending_review WHERE field = 'cross_service_merge'"
    ).fetchall()
    assert {r["entity_id"] for r in reviews} == {"s-swpl02", "s-swpl03"}


def test_sweep_show_merges_isolates_one_pairs_failure(conn, monkeypatch):
    _show(conn, "s-swpl02", "Sweep Show Two", tracking_space="tv")
    _external_id(conn, "s-swpl02", "tvdb", "111")
    _show(conn, "s-swpw02", "Sweep Show Two", tracking_space="anime")
    _external_id(conn, "s-swpw02", "anilist", "222")
    conn.commit()

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(show_merge.pending_review, "open_or_extend", _boom)
    result = show_merge.sweep_show_merges(conn)
    assert result == {"candidates_found": 1, "reviews_opened": 0}
    assert (
        conn.execute("SELECT * FROM pending_review WHERE entity_id = 's-swpl02'").fetchone() is None
    )


# --- apply_show_merge (2026-08-12) ----------------------------------------------


def test_apply_show_merge_performs_the_merge_and_resolves_the_review(conn):
    _show(conn, "s-appl01", "Apply Me", tracking_space="tv")
    _external_id(conn, "s-appl01", "tvdb", "111")
    _show(conn, "s-appw01", "Apply Me", tracking_space="anime")
    _external_id(conn, "s-appw01", "anilist", "222")
    conn.commit()
    show_merge.sweep_show_merges(conn)  # opens the review this call should resolve

    merge_id = show_merge.apply_show_merge(conn, "s-appw01", "s-appl01", "manual review", "data")

    assert conn.execute("SELECT tracked FROM show WHERE id = 's-appl01'").fetchone()["tracked"] == 0
    assert (
        conn.execute("SELECT id FROM show_merge WHERE id = ?", (merge_id,)).fetchone() is not None
    )
    review = conn.execute(
        "SELECT resolved_at, resolved_by_client, resolution_note FROM pending_review"
        " WHERE entity_id = 's-appl01' AND field = 'cross_service_merge'"
    ).fetchone()
    assert review["resolved_at"] is not None
    assert review["resolved_by_client"] == "data"
    assert "s-appw01" in review["resolution_note"]


def test_apply_show_merge_refuses_a_winner_that_already_absorbed_a_different_loser(conn):
    """The real orphaning bug the old sweep's claimed_winner_ids guard
    existed for — now guarded here instead, since this is the only
    place a merge actually happens anymore."""
    _show(conn, "s-appw02", "Twice Claimed", tracking_space="anime")
    _external_id(conn, "s-appw02", "anilist", "999")
    _show(conn, "s-appl02", "Twice Claimed", tracking_space="tv")
    _external_id(conn, "s-appl02", "tvdb", "111")
    _show(conn, "s-appl03", "Twice Claimed", tracking_space="tv")
    _external_id(conn, "s-appl03", "tvdb", "222")
    conn.commit()
    show_merge.apply_show_merge(conn, "s-appw02", "s-appl02", "manual review", "data")

    with pytest.raises(ValueError, match="already has a linked"):
        show_merge.apply_show_merge(conn, "s-appw02", "s-appl03", "manual review", "data")

    # The second loser is untouched — still tracked, still holding its own link.
    row = conn.execute("SELECT tracked FROM show WHERE id = 's-appl03'").fetchone()
    assert row["tracked"] == 1


# --- reverse_show_merge ---------------------------------------------------------


def test_reverse_show_merge_restores_external_ids_episodes_and_tracked(conn):
    _show(conn, "s-winrv1", "Winner")
    _show(conn, "s-losrv1", "Loser", tracking_space="tv")
    _external_id(conn, "s-losrv1", "tvdb", "111")
    _episode(conn, "e-eprv01", "s-losrv1", 1, 1)
    _watch_event(conn, "w-wervv1", "s-losrv1", 1, 1)
    conn.commit()

    merge_id = show_merge.merge_shows(conn, "s-winrv1", "s-losrv1", "test")
    show_merge.reverse_show_merge(conn, merge_id, "data")

    assert conn.execute("SELECT tracked FROM show WHERE id = 's-losrv1'").fetchone()["tracked"] == 1
    assert (
        conn.execute("SELECT show_id FROM show_external_id WHERE service = 'tvdb'").fetchone()[
            "show_id"
        ]
        == "s-losrv1"
    )
    assert (
        conn.execute("SELECT show_id FROM episode WHERE id = 'e-eprv01'").fetchone()["show_id"]
        == "s-losrv1"
    )
    assert (
        conn.execute("SELECT show_id FROM watch_event WHERE id = 'w-wervv1'").fetchone()["show_id"]
        == "s-losrv1"
    )
    reversed_row = conn.execute(
        "SELECT reversed_at, reversed_by_client FROM show_merge WHERE id = ?", (merge_id,)
    ).fetchone()
    assert reversed_row["reversed_at"] is not None
    assert reversed_row["reversed_by_client"] == "data"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reverse_show_merge_restores_a_repointed_seasons_episode(conn):
    _show(conn, "s-winrv2", "Winner")
    _season(conn, "z-sewrv2", "s-winrv2", 1)
    _show(conn, "s-losrv2", "Loser", tracking_space="tv")
    _season(conn, "z-selrv2", "s-losrv2", 1)
    _episode(conn, "e-eprv02", "s-losrv2", 1, 1, season_id="z-selrv2")
    conn.commit()

    merge_id = show_merge.merge_shows(conn, "s-winrv2", "s-losrv2", "test")
    show_merge.reverse_show_merge(conn, merge_id, "data")

    restored = conn.execute(
        "SELECT show_id, season_id FROM episode WHERE id = 'e-eprv02'"
    ).fetchone()
    assert restored["show_id"] == "s-losrv2"
    assert restored["season_id"] == "z-selrv2"


def test_reverse_show_merge_raises_for_unknown_id(conn):
    with pytest.raises(ValueError, match="no such show_merge"):
        show_merge.reverse_show_merge(conn, "y-ghost1", "data")


def test_reverse_show_merge_raises_if_already_reversed(conn):
    _show(conn, "s-winrv3", "Winner")
    _show(conn, "s-losrv3", "Loser", tracking_space="tv")
    conn.commit()
    merge_id = show_merge.merge_shows(conn, "s-winrv3", "s-losrv3", "test")
    show_merge.reverse_show_merge(conn, merge_id, "data")
    with pytest.raises(ValueError, match="already reversed"):
        show_merge.reverse_show_merge(conn, merge_id, "data")


def test_merge_clears_season_external_id_for_skipped_seasons(conn):
    """A skipped season (winner already has the same season_number) has its
    episodes repointed to the winner's season and becomes a ghost row. Its
    season_external_id rows must be deleted so _apply_remote_list's dup-id
    guard is never triggered by the leftover ghost mapping."""
    _show(conn, "s-winsid", "Winner")
    _season(conn, "z-seawid", "s-winsid", 1)
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES ('z-seawid', 'anilist', 999, 'x')"
    )
    _show(conn, "s-lossid", "Loser", tracking_space="tv")
    _season(conn, "z-sealid", "s-lossid", 1)  # conflicts with winner's season 1
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES ('z-sealid', 'anilist', 999, 'x')"  # same external_id — would trigger dup guard
    )
    conn.commit()

    show_merge.merge_shows(conn, "s-winsid", "s-lossid", "test")

    # Loser's skipped season mapping must be gone
    loser_mapping = conn.execute(
        "SELECT COUNT(*) FROM season_external_id WHERE season_id = 'z-sealid'"
    ).fetchone()[0]
    assert loser_mapping == 0, "ghost season's season_external_id rows should have been deleted"

    # Winner's own mapping must be untouched
    winner_mapping = conn.execute(
        "SELECT external_id FROM season_external_id WHERE season_id = 'z-seawid'"
    ).fetchone()
    assert winner_mapping is not None and winner_mapping["external_id"] == 999


def test_merge_preserves_season_external_id_for_moved_seasons(conn):
    """A non-conflicting season that moves to the winner must carry its
    season_external_id rows with it — they reference season.id, which is
    unchanged by the show_id UPDATE, so they follow for free."""
    _show(conn, "s-winmid", "Winner")
    _show(conn, "s-losmid", "Loser", tracking_space="tv")
    _season(conn, "z-sealmd", "s-losmid", 1)
    conn.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES ('z-sealmd', 'anilist', 888, 'x')"
    )
    conn.commit()

    show_merge.merge_shows(conn, "s-winmid", "s-losmid", "test")

    # Season moved to winner
    moved_season = conn.execute("SELECT show_id FROM season WHERE id = 'z-sealmd'").fetchone()
    assert moved_season["show_id"] == "s-winmid"

    # Mapping row is still there, now pointing at the moved season (which is on the winner)
    mapping = conn.execute(
        "SELECT external_id FROM season_external_id WHERE season_id = 'z-sealmd'"
    ).fetchone()
    assert mapping is not None and mapping["external_id"] == 888
