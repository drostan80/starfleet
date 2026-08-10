"""episode_movie_link automatic tmdb_match derivation — SCOPE.md
§5.1's movie <-> bonus_movie addendum, BUILD_PLAN.md B.8b. Same
real-migrated-SQLite-DB approach test_animeschedule.py/
test_service_presence.py already established, no fake client needed —
this module makes no outbound HTTP calls at all.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import episode_movie_link


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "episode_movie_link_test.db"
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


def _add_show(
    conn,
    show_id,
    title_romaji="Test Show",
    media_shape="episodic",
    tracked=True,
    available_via_radarr="unavailable",
    file_path_radarr=None,
):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, available_via_radarr, file_path_radarr, created_at, updated_at)"
        " VALUES (?, ?, 'anime', ?, 'romaji', 'watching', ?, ?, ?, 'x', 'x')",
        (show_id, media_shape, title_romaji, int(tracked), available_via_radarr, file_path_radarr),
    )
    conn.commit()


def _add_episode(conn, episode_id, show_id, season=0, episode=1, kind="bonus_movie"):
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'unwatched', 'x', 'x')",
        (episode_id, show_id, season, episode, kind),
    )
    conn.commit()


def _relate(conn, show_id, related_show_id):
    conn.execute(
        "INSERT INTO show_relation (show_id, related_show_id, created_at) VALUES (?, ?, 'x')",
        (show_id, related_show_id),
    )
    conn.commit()


def _link(conn, episode_id):
    return conn.execute(
        "SELECT * FROM episode_movie_link WHERE episode_id = ?", (episode_id,)
    ).fetchone()


def _pending_reviews(conn, entity_id, field):
    return conn.execute(
        "SELECT * FROM pending_review WHERE entity_id = ? AND field = ?", (entity_id, field)
    ).fetchall()


# --- derivation: candidate pool / apply / flag / unmatched --------------------


def test_exactly_one_relation_graph_candidate_matches_immediately(conn):
    _add_show(conn, "s-eml001", title_romaji="Parent Show")
    _add_show(conn, "s-eml002", title_romaji="The Movie", media_shape="movie")
    _add_episode(conn, "e-eml001", "s-eml001")
    _relate(conn, "s-eml001", "s-eml002")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["matched"] == 1
    assert result["flagged"] == 0
    assert result["unmatched"] == 0

    link = _link(conn, "e-eml001")
    assert link["movie_show_id"] == "s-eml002"
    assert link["source"] == "tmdb_match"
    assert link["matched"] == 1


def test_relation_is_checked_in_either_direction(conn):
    _add_show(conn, "s-eml003", title_romaji="Parent Show")
    _add_show(conn, "s-eml004", title_romaji="The Movie", media_shape="movie")
    _add_episode(conn, "e-eml002", "s-eml003")
    # the relation edge was written the other direction (metadata._link_relation
    # writes one direction per fetch — either side's own fetch could be first)
    _relate(conn, "s-eml004", "s-eml003")

    episode_movie_link.reconcile_episode_movie_links(conn)
    assert _link(conn, "e-eml002")["movie_show_id"] == "s-eml004"


def test_zero_candidates_records_a_real_unmatched_row(conn):
    _add_show(conn, "s-eml005", title_romaji="Parent Show")
    _add_episode(conn, "e-eml003", "s-eml005")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["unmatched"] == 1
    link = _link(conn, "e-eml003")
    assert link["source"] == "unmatched"
    assert link["movie_show_id"] is None
    assert link["matched"] == 0


def test_more_than_one_candidate_flags_a_pending_review(conn):
    _add_show(conn, "s-eml006", title_romaji="Parent Show")
    _add_show(conn, "s-eml007", title_romaji="Movie One", media_shape="movie")
    _add_show(conn, "s-eml008", title_romaji="Movie Two", media_shape="movie")
    _add_episode(conn, "e-eml004", "s-eml006")
    _relate(conn, "s-eml006", "s-eml007")
    _relate(conn, "s-eml006", "s-eml008")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["flagged"] == 1
    assert _link(conn, "e-eml004") is None  # no episode_movie_link row written at all
    reviews = _pending_reviews(conn, "e-eml004", "episode_movie_link")
    assert len(reviews) == 1
    assert "Movie One" in reviews[0]["proposed_value_chain"]
    assert "Movie Two" in reviews[0]["proposed_value_chain"]


def test_repeat_sweep_with_the_same_ambiguous_candidates_does_not_duplicate_the_chain(conn):
    _add_show(conn, "s-eml009", title_romaji="Parent Show")
    _add_show(conn, "s-eml010", title_romaji="Movie One", media_shape="movie")
    _add_show(conn, "s-eml011", title_romaji="Movie Two", media_shape="movie")
    _add_episode(conn, "e-eml005", "s-eml009")
    _relate(conn, "s-eml009", "s-eml010")
    _relate(conn, "s-eml009", "s-eml011")

    episode_movie_link.reconcile_episode_movie_links(conn)
    second = episode_movie_link.reconcile_episode_movie_links(conn)
    assert second["flagged"] == 0  # same finding re-seen, not a new one

    reviews = _pending_reviews(conn, "e-eml005", "episode_movie_link")
    assert len(reviews) == 1
    chain = json.loads(reviews[0]["proposed_value_chain"])
    assert len(chain) == 1  # not appended a second, identical entry


def test_non_bonus_movie_episodes_are_never_processed(conn):
    _add_show(conn, "s-eml012", title_romaji="Parent Show")
    _add_show(conn, "s-eml013", title_romaji="The Movie", media_shape="movie")
    _add_episode(conn, "e-eml006", "s-eml012", season=1, episode=1, kind="regular")
    _relate(conn, "s-eml012", "s-eml013")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result == {"matched": 0, "flagged": 0, "unmatched": 0, "availability_synced": 0}
    assert _link(conn, "e-eml006") is None


def test_untracked_candidate_movie_show_is_not_counted(conn):
    _add_show(conn, "s-eml014", title_romaji="Parent Show")
    _add_show(conn, "s-eml015", title_romaji="The Movie", media_shape="movie", tracked=False)
    _add_episode(conn, "e-eml007", "s-eml014")
    _relate(conn, "s-eml014", "s-eml015")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["unmatched"] == 1  # the only candidate is untracked, so none count


def test_episodic_related_show_is_not_a_candidate(conn):
    _add_show(conn, "s-eml016", title_romaji="Parent Show")
    _add_show(conn, "s-eml017", title_romaji="Related Series", media_shape="episodic")
    _add_episode(conn, "e-eml008", "s-eml016")
    _relate(conn, "s-eml016", "s-eml017")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["unmatched"] == 1


def test_a_manual_override_link_is_never_touched(conn):
    _add_show(conn, "s-eml018", title_romaji="Parent Show")
    _add_show(conn, "s-eml019", title_romaji="Wrong Movie", media_shape="movie")
    _add_show(conn, "s-eml020", title_romaji="Right Movie", media_shape="movie")
    _add_episode(conn, "e-eml009", "s-eml018")
    _relate(conn, "s-eml018", "s-eml019")
    conn.execute(
        "INSERT INTO episode_movie_link"
        " (id, episode_id, movie_show_id, source, matched, manual_override,"
        "  created_at, updated_at)"
        " VALUES ('m-manul1', 'e-eml009', 's-eml020', 'manual', 1, 1, 'x', 'x')"
    )
    conn.commit()

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result == {"matched": 0, "flagged": 0, "unmatched": 0, "availability_synced": 0}
    assert _link(conn, "e-eml009")["movie_show_id"] == "s-eml020"


def test_a_stable_automatic_match_is_not_rewritten_on_a_repeat_sweep(conn):
    _add_show(conn, "s-eml021", title_romaji="Parent Show")
    _add_show(conn, "s-eml022", title_romaji="The Movie", media_shape="movie")
    _add_episode(conn, "e-eml010", "s-eml021")
    _relate(conn, "s-eml021", "s-eml022")

    episode_movie_link.reconcile_episode_movie_links(conn)
    first_updated_at = _link(conn, "e-eml010")["updated_at"]
    second = episode_movie_link.reconcile_episode_movie_links(conn)
    assert second["matched"] == 0
    assert _link(conn, "e-eml010")["updated_at"] == first_updated_at


def test_a_previously_unmatched_episode_self_heals_once_a_relation_edge_appears(conn):
    _add_show(conn, "s-eml023", title_romaji="Parent Show")
    _add_episode(conn, "e-eml011", "s-eml023")
    episode_movie_link.reconcile_episode_movie_links(conn)
    assert _link(conn, "e-eml011")["source"] == "unmatched"

    _add_show(conn, "s-eml024", title_romaji="The Movie", media_shape="movie")
    _relate(conn, "s-eml023", "s-eml024")
    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["matched"] == 1
    assert _link(conn, "e-eml011")["movie_show_id"] == "s-eml024"


# --- availability sync ---------------------------------------------------------


def test_a_matched_links_availability_is_mirrored_onto_the_episode(conn):
    _add_show(conn, "s-eml025", title_romaji="Parent Show")
    _add_show(
        conn,
        "s-eml026",
        title_romaji="The Movie",
        media_shape="movie",
        available_via_radarr="available",
        file_path_radarr="/movies/the-movie.mkv",
    )
    _add_episode(conn, "e-eml012", "s-eml025")
    _relate(conn, "s-eml025", "s-eml026")

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["availability_synced"] == 1

    ep = conn.execute("SELECT * FROM episode WHERE id = 'e-eml012'").fetchone()
    assert ep["available_via_radarr"] == "available"
    assert ep["file_path_radarr"] == "/movies/the-movie.mkv"


def test_availability_sync_also_applies_to_a_manual_link(conn):
    _add_show(conn, "s-eml027", title_romaji="Parent Show")
    _add_show(
        conn,
        "s-eml028",
        title_romaji="Right Movie",
        media_shape="movie",
        available_via_radarr="downloading",
    )
    _add_episode(conn, "e-eml013", "s-eml027")
    conn.execute(
        "INSERT INTO episode_movie_link"
        " (id, episode_id, movie_show_id, source, matched, manual_override,"
        "  created_at, updated_at)"
        " VALUES ('m-manul2', 'e-eml013', 's-eml028', 'manual', 1, 1, 'x', 'x')"
    )
    conn.commit()

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["availability_synced"] == 1
    ep = conn.execute("SELECT * FROM episode WHERE id = 'e-eml013'").fetchone()
    assert ep["available_via_radarr"] == "downloading"


def test_availability_sync_is_a_no_op_once_already_in_sync(conn):
    _add_show(conn, "s-eml029", title_romaji="Parent Show")
    _add_show(
        conn,
        "s-eml030",
        title_romaji="The Movie",
        media_shape="movie",
        available_via_radarr="available",
        file_path_radarr="/movies/x.mkv",
    )
    _add_episode(conn, "e-eml014", "s-eml029")
    _relate(conn, "s-eml029", "s-eml030")

    episode_movie_link.reconcile_episode_movie_links(conn)
    ep_before = conn.execute("SELECT updated_at FROM episode WHERE id = 'e-eml014'").fetchone()
    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["availability_synced"] == 0
    ep_after = conn.execute("SELECT updated_at FROM episode WHERE id = 'e-eml014'").fetchone()
    assert ep_after["updated_at"] == ep_before["updated_at"]


def test_an_unmatched_episode_has_nothing_to_sync(conn):
    _add_show(conn, "s-eml031", title_romaji="Parent Show")
    _add_episode(conn, "e-eml015", "s-eml031")
    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["availability_synced"] == 0


def test_a_manual_link_against_a_non_bonus_movie_episode_is_never_synced(conn):
    # Caught in review before commit: setEpisodeMovieLink (A.3, manual) has no
    # kind guard at all, and available_via_radarr feeds a *generated*
    # available_locally column (§5.2) — writing it onto a regular/special
    # episode would be a real, silent wrong-availability bug, not a no-op.
    _add_show(conn, "s-eml032", title_romaji="Parent Show")
    _add_show(
        conn,
        "s-eml033",
        title_romaji="The Movie",
        media_shape="movie",
        available_via_radarr="available",
        file_path_radarr="/movies/x.mkv",
    )
    _add_episode(conn, "e-eml016", "s-eml032", season=1, episode=1, kind="regular")
    conn.execute(
        "INSERT INTO episode_movie_link"
        " (id, episode_id, movie_show_id, source, matched, manual_override,"
        "  created_at, updated_at)"
        " VALUES ('m-manul3', 'e-eml016', 's-eml033', 'manual', 1, 1, 'x', 'x')"
    )
    conn.commit()

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["availability_synced"] == 0
    ep = conn.execute("SELECT * FROM episode WHERE id = 'e-eml016'").fetchone()
    assert ep["available_via_radarr"] == "unavailable"
    assert ep["available_locally"] == 0


def test_a_manual_link_against_a_non_movie_show_is_never_synced(conn):
    # Same guard, the other side: setEpisodeMovieLink also never checks
    # media_shape, so a bad manual link could point at an episodic show whose
    # own available_via_radarr is meaningless (§5.1: movie-only field).
    _add_show(conn, "s-eml034", title_romaji="Parent Show")
    _add_show(
        conn,
        "s-eml035",
        title_romaji="Not Actually A Movie",
        media_shape="episodic",
        available_via_radarr="available",
        file_path_radarr="/movies/x.mkv",
    )
    _add_episode(conn, "e-eml017", "s-eml034")
    conn.execute(
        "INSERT INTO episode_movie_link"
        " (id, episode_id, movie_show_id, source, matched, manual_override,"
        "  created_at, updated_at)"
        " VALUES ('m-manul4', 'e-eml017', 's-eml035', 'manual', 1, 1, 'x', 'x')"
    )
    conn.commit()

    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["availability_synced"] == 0
    ep = conn.execute("SELECT * FROM episode WHERE id = 'e-eml017'").fetchone()
    assert ep["available_via_radarr"] == "unavailable"


def test_a_matched_episode_flips_back_to_unmatched_once_its_candidate_is_untracked(conn):
    _add_show(conn, "s-eml036", title_romaji="Parent Show")
    _add_show(conn, "s-eml037", title_romaji="The Movie", media_shape="movie")
    _add_episode(conn, "e-eml018", "s-eml036")
    _relate(conn, "s-eml036", "s-eml037")

    episode_movie_link.reconcile_episode_movie_links(conn)
    assert _link(conn, "e-eml018")["source"] == "tmdb_match"

    conn.execute("UPDATE show SET tracked = 0 WHERE id = 's-eml037'")
    conn.commit()
    result = episode_movie_link.reconcile_episode_movie_links(conn)
    assert result["unmatched"] == 1
    link = _link(conn, "e-eml018")
    assert link["source"] == "unmatched"
    assert link["movie_show_id"] is None
