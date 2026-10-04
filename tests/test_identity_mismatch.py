"""identity_mismatch.check_anilist_id_mismatch — 2026-09-21, found via the
real Tantei wa mou, Shindeiru. Season 2 incident (a show wrongly linked
to Detective Opera Milky Holmes' Sonarr series). Same real-migrated-
SQLite-DB + fribb.load_dataset-monkeypatched approach test_show_backfill.py
already established — no real network call.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import fribb, identity_mismatch


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "identity_mismatch_test.db"
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


def _patch_fribb(monkeypatch, dataset):
    monkeypatch.setattr(fribb, "load_dataset", lambda: dataset)


def _show(conn, show_id, tvdb_id, title="Show", manual_override=0):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES (?, 'episodic', 'anime', ?, 'romaji', 'watching', 1, 'x', 'x')",
        (show_id, title),
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'tvdb', ?, 'x', 'x')",
        (show_id, tvdb_id),
    )


def _season(conn, season_id, show_id, season_number, anilist_id, manual_override=0):
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, source, matched, manual_override,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 1, ?, 'x', 'x')",
        (season_id, show_id, season_number, anilist_id, manual_override),
    )


def _fribb_entry(
    tvdb_id, anilist_id, mal_id=None, tvdb_season=1, entry_type="TV", episode_offset=None
):
    entry = {
        "tvdb_id": tvdb_id,
        "type": entry_type,
        "anilist_id": anilist_id,
        "mal_id": mal_id,
        "season": {"tvdb": tvdb_season},
    }
    if episode_offset is not None:
        entry["episode_offset"] = {"tvdb": episode_offset}
    return entry


def test_flags_a_real_mismatch(conn, monkeypatch):
    # The actual shape of the Tantei/Milky-Holmes incident: stored
    # anilist_id (152677, Tantei) disagrees with what Fribb independently
    # resolves for this tvdb_id (7768, a different show entirely).
    _show(conn, "s-wrng01", tvdb_id=197261)
    _season(conn, "z-wrng01", "s-wrng01", 1, anilist_id=152677)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(197261, 7768)])

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 1}
    review = conn.execute(
        "SELECT field, source, previous_value, proposed_value_chain FROM pending_review"
        " WHERE entity_type = 'season' AND entity_id = 'z-wrng01' AND resolved_at IS NULL"
    ).fetchone()
    assert review is not None
    assert review["field"] == "anilist_id"
    assert review["source"] == "fribb_identity_mismatch"
    assert review["previous_value"] == "152677"
    assert json.loads(review["proposed_value_chain"]) == ["7768"]


def test_no_flag_when_ids_agree(conn, monkeypatch):
    _show(conn, "s-rght01", tvdb_id=300001)
    _season(conn, "z-rght01", "s-rght01", 1, anilist_id=555)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(300001, 555)])

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 0}
    assert (
        conn.execute("SELECT * FROM pending_review WHERE entity_id = 'z-rght01'").fetchone()
        is None
    )


def test_no_flag_when_fribb_has_no_opinion(conn, monkeypatch):
    _show(conn, "s-unknw1", tvdb_id=400001)
    _season(conn, "z-unknw1", "s-unknw1", 1, anilist_id=999)
    conn.commit()
    _patch_fribb(monkeypatch, [])  # Fribb dataset empty — no opinion either way

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 0, "flagged": 0}


def test_checks_seasons_regardless_of_manual_override(conn, monkeypatch):
    # The real Tantei season row already had manual_override=1 set on
    # the wrong value before the bug was found — trusting that flag
    # would have missed exactly the case this sweep exists to catch.
    _show(conn, "s-wrng02", tvdb_id=197262)
    _season(conn, "z-wrng02", "s-wrng02", 1, anilist_id=152677, manual_override=1)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(197262, 7768)])

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 1}


def test_does_not_reflag_after_human_resolves_same_value(conn, monkeypatch):
    _show(conn, "s-wrng03", tvdb_id=197263)
    _season(conn, "z-wrng03", "s-wrng03", 1, anilist_id=152677)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(197263, 7768)])

    identity_mismatch.check_anilist_id_mismatch(conn)
    conn.execute(
        "UPDATE pending_review SET resolved_at = 'now', resolved_by_client = 'holodeck'"
        " WHERE entity_id = 'z-wrng03'"
    )
    conn.commit()

    # Next tick: same disagreement, same proposed value — must not reopen.
    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 0}
    open_count = conn.execute(
        "SELECT COUNT(*) FROM pending_review"
        " WHERE entity_id = 'z-wrng03' AND resolved_at IS NULL"
    ).fetchone()[0]
    assert open_count == 0


def test_reopens_when_disagreement_changes_after_a_resolved_fix(conn, monkeypatch):
    # User's own explicit confirmation of the wanted behavior: a
    # genuinely new mismatch value must reopen even after a manual fix
    # was already resolved once — "I would have to go back manually and
    # change it again if I find out it was still wrong after correction."
    _show(conn, "s-wrng04", tvdb_id=197264)
    _season(conn, "z-wrng04", "s-wrng04", 1, anilist_id=152677)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(197264, 7768)])
    identity_mismatch.check_anilist_id_mismatch(conn)
    conn.execute(
        "UPDATE pending_review SET resolved_at = 'now', resolved_by_client = 'holodeck'"
        " WHERE entity_id = 'z-wrng04'"
    )
    # Simulate the human's fix: season now stores Fribb's proposed value.
    conn.execute("UPDATE season SET anilist_id = 7768 WHERE id = 'z-wrng04'")
    conn.commit()

    # Fribb's own data changes (e.g. dataset correction) — a genuinely
    # different disagreement now exists.
    _patch_fribb(monkeypatch, [_fribb_entry(197264, 8888)])
    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 1}
    review = conn.execute(
        "SELECT previous_value FROM pending_review"
        " WHERE entity_id = 'z-wrng04' AND resolved_at IS NULL"
    ).fetchone()
    assert review["previous_value"] == "7768"


def test_no_op_when_show_has_no_tvdb_id(conn, monkeypatch):
    conn.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, tracked, created_at, updated_at)"
        " VALUES ('s-notvd1', 'episodic', 'anime', 'Show', 'romaji', 'watching', 1, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, source, created_at, updated_at)"
        " VALUES ('z-notvd1', 's-notvd1', 1, 123, 'manual', 'x', 'x')"
    )
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(999999, 456)])

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 0, "flagged": 0}


def _part(conn, season_id, show_id, parent_id, part_number, anilist_id, season_number=1):
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, part_number, anilist_id, source, matched, manual_override,"
        "  kind, parent_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'manual', 1, 0, 'part', ?, 'x', 'x')",
        (season_id, show_id, season_number, part_number, anilist_id, parent_id),
    )


def test_split_cour_gives_no_opinion_and_its_parts_are_not_checked(conn, monkeypatch):
    # SPY x FAMILY on the rebuilt data: TVDB season 1 holds two cours (Fribb tags both
    # season.tvdb=1, told apart only by episode_offset) — in LCARS two `part` levels under
    # TVDB season 1 — and TVDB season 2 is its own entry. The split season has several Fribb
    # entries: no opinion; its parts are not compared at all; season 2 is checked and agrees.
    _show(conn, "s-spyfa1", tvdb_id=405920)
    _season(conn, "z-spyfa1", "s-spyfa1", 1, anilist_id=140960)
    _part(conn, "z-spyfa2", "s-spyfa1", "z-spyfa1", 2, anilist_id=142838)
    _season(conn, "z-spyfa3", "s-spyfa1", 2, anilist_id=158927)
    conn.commit()
    _patch_fribb(
        monkeypatch,
        [
            _fribb_entry(405920, 140960, tvdb_season=1),
            _fribb_entry(405920, 142838, tvdb_season=1, episode_offset=12),
            _fribb_entry(405920, 158927, tvdb_season=2),
            _fribb_entry(405920, 999999, tvdb_season=0, entry_type="MOVIE"),  # movie, excluded
        ],
    )

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 0}
    assert conn.execute("SELECT * FROM pending_review").fetchone() is None


def test_a_whole_season_with_the_wrong_id_is_still_caught(conn, monkeypatch):
    # TVDB season 2 has exactly one Fribb entry; LCARS holds a different AniList id for it.
    _show(conn, "s-spyfa4", tvdb_id=405921)
    _season(conn, "z-spyfa4", "s-spyfa4", 1, anilist_id=140960)
    _season(conn, "z-spyfa5", "s-spyfa4", 2, anilist_id=999111)  # wrong
    conn.commit()
    _patch_fribb(
        monkeypatch,
        [
            _fribb_entry(405921, 140960, tvdb_season=1),
            _fribb_entry(405921, 158927, tvdb_season=2),
        ],
    )

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 2, "flagged": 1}
    review = conn.execute(
        "SELECT proposed_value_chain FROM pending_review WHERE entity_id = 'z-spyfa5'"
    ).fetchone()
    assert json.loads(review["proposed_value_chain"]) == ["158927"]


def test_a_tvdb_season_fribb_has_no_entry_for_gives_no_opinion(conn, monkeypatch):
    # Gintama on the rebuilt data: a row for every TVDB season (1–10) but Fribb lists only some.
    # The old position method read "season 5" as "Fribb's 5th entry" and flagged it.
    _show(conn, "s-gin001", tvdb_id=78914)
    _season(conn, "z-gin001", "s-gin001", 1, anilist_id=918)
    _season(conn, "z-gin005", "s-gin001", 5, anilist_id=9969)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(78914, 9969, tvdb_season=2)])  # no entry for 1 or 5

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 0, "flagged": 0}
    assert conn.execute("SELECT * FROM pending_review").fetchone() is None


def test_a_level_with_no_season_number_is_skipped_not_a_crash(conn, monkeypatch):
    # Specials and other levels carry no season number; the check used to fail on `None < 1`
    # on every hourly sweep (since the 09-30 cutover). They are simply not checked.
    _show(conn, "s-nonum1", tvdb_id=197261)
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, anilist_id, source, matched,"
        " manual_override, kind, created_at, updated_at)"
        " VALUES ('z-nonum1', 's-nonum1', NULL, 4242, 'manual', 1, 0, 'special', 'x', 'x')"
    )
    _season(conn, "z-nonum2", "s-nonum1", 1, anilist_id=152677)
    conn.commit()
    _patch_fribb(monkeypatch, [_fribb_entry(197261, 7768)])

    result = identity_mismatch.check_anilist_id_mismatch(conn)

    assert result == {"checked": 1, "flagged": 1}  # the numbered season is still checked
