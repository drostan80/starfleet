"""scripts/cleanup_noise_reviews_20261005.py — resolves the noise classes, leaves real reviews."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "cleanup_noise", ROOT / "scripts" / "cleanup_noise_reviews_20261005.py")
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)

T = "2026-10-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "noise.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    for sid in ("s-noi001", "s-noi002"):
        c.execute(
            "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
            " status, tracked, created_at, updated_at)"
            " VALUES (?, 'episodic', 'anime', 'N', 'romaji', 'watching', 1, ?, ?)", (sid, T, T))
    for zid, show, n, total, al in (("z-noi001", "s-noi001", 1, 195, 1293),
                                    ("z-noi002", "s-noi001", 2, 195, 1293),
                                    ("z-noi003", "s-noi002", 1, 5, 777)):
        c.execute(
            "INSERT INTO season (id, show_id, season_number, status, anilist_id, episode_total,"
            " source, created_at, updated_at)"
            " VALUES (?, ?, ?, 'completed', ?, ?, 'manual', ?, ?)",
            (zid, show, n, al, total, T, T))
        c.execute("INSERT INTO season_external_id (season_id, service, external_id, created_at)"
                  " VALUES (?, 'anilist', ?, ?)", (zid, al, T))
    c.commit()
    return c


_n = iter(range(1, 999))


def _review(c, field, chain, entity="z-noi001", etype="season", source="anilist", prev=None):
    rid = f"r-nz{next(_n):04d}"
    c.execute(
        "INSERT INTO pending_review (id, entity_type, entity_id, field, previous_value,"
        " proposed_value_chain, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, etype, entity, field, prev, json.dumps(chain), source, T))
    return rid


def _open_ids(c):
    return {r[0] for r in c.execute("SELECT id FROM pending_review WHERE resolved_at IS NULL")}


def test_every_noise_class_is_resolved_and_real_reviews_stay(conn):
    noise = [
        _review(conn, "anilist_id_conflict", ["x"], source="anilist_reconcile"),  # one show, shared
        _review(conn, "season_subdivision", ["anilist=2,range_width=68"],
                source="anilist_width_check"),
        _review(conn, "anilist_id", ["unmatched"], source="fribb", entity="z-noi002"),
        _review(conn, "air_date_utc", ["AniList proposes 2026-09-27T15:00:00Z (a delay past the"
                " current 2026-09-27T14:30:00Z) but a file is already downloaded"],
                etype="episode", entity="e-xxxxxxxx"),
        _review(conn, "air_date_utc", ["2026-10-03T13:30:00Z"], etype="episode",
                source="animeschedule", entity="e-xxxxxxxx"),
        _review(conn, "anilist_push", ["Could not connect to AniList"]),
        _review(conn, "metadata_fetch", ["AniList returned an error: HTTP 429"], etype="show",
                entity="s-noi001"),
        _review(conn, "scheme", ["absolute"], etype="episode_numbering_mapping",
                entity="n-xxxxxxx", source="sonarr", prev="season_episode"),
        _review(conn, "anilist_id", ["no AniList match for tvdb id 1 (§5.1 requires one)"],
                etype="show", entity="s-noi001", source="fribb"),
        _review(conn, "list_readback_differs", ["mal holds progress 5 (LCARS 9) after LCARS wrote"
                " its value"], entity="z-noi003", source="mal"),
        _review(conn, "animeschedule_episode_match", ["matched 2 candidate episode row(s)"],
                etype="show", entity="s-noi001", source="animeschedule"),
    ]
    keep = [
        _review(conn, "anilist_push", ["AniList returned an error: invalid media id"]),  # real
        _review(conn, "anilist_id", ["season 1: AniList media 211877's airing schedule is ~5451"
                " days from Sonarr's own dates"], source="anilist"),
        _review(conn, "air_date_utc", ["2026-12-01T00:00:00Z"], etype="episode",
                source="anilist", entity="e-xxxxxxxx"),  # not a "delay past" message
        _review(conn, "list_readback_differs", ["mal holds progress 3 (LCARS 9) after LCARS wrote"
                " its value"], entity="z-noi003", source="mal"),  # not the clamp (total is 5)
        _review(conn, "remote_progress_lower", ["mal says 3, LCARS has 4 watched"] * 3,
                source="mal_reconcile"),
    ]
    conn.commit()

    dry = cleanup.run(conn, apply=False)
    assert dry["counts"] == {"conflict": 1, "width": 1, "unmatched": 1, "airdate": 2,
                             "transient": 2, "scheme": 1, "nomatch": 1, "clamp": 1, "ambiguous": 1}
    assert _open_ids(conn) == set(noise) | set(keep)  # a dry run writes nothing

    done = cleanup.run(conn, apply=True)
    assert done["applied"] is True
    assert _open_ids(conn) == set(keep)
    note = conn.execute("SELECT resolution_note FROM pending_review WHERE id = ?",
                        (noise[0],)).fetchone()[0]
    assert "conflict" in note and "2026-10-05" in note
    # the kept chain with three identical messages is collapsed to one
    chain = json.loads(conn.execute("SELECT proposed_value_chain FROM pending_review"
                                    " WHERE field = 'remote_progress_lower'").fetchone()[0])
    assert chain == ["mal says 3, LCARS has 4 watched"]
    assert cleanup.run(conn, apply=True)["counts"] == {k: 0 for k in dry["counts"]}


def test_two_different_shows_sharing_an_id_stay_a_conflict(conn):
    conn.execute("UPDATE season_external_id SET external_id = 1293 WHERE season_id = 'z-noi003'")
    rid = _review(conn, "anilist_id_conflict", ["x"], entity="z-noi001", source="anilist_reconcile")
    conn.commit()
    assert cleanup.run(conn, apply=True)["counts"]["conflict"] == 0
    assert rid in _open_ids(conn)
