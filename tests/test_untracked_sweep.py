"""untracked_sweep.py — SCOPE.md §5.2's "Resolved 2026-08-10 (B.11
reconnaissance)" note, BUILD_PLAN.md B.11e.
`show_backfill.preview_backfill_with_status` is monkeypatched directly
throughout — its own classification/dedup logic is already covered by
test_show_backfill.py; these tests are about this module's own
reconciliation against untracked_show_finding (insert/refresh/prune,
and the per-source pruning gate itself — B.11e follow-up, a real bug
found in review), not re-deriving what "untracked" means. Same
real-migrated-SQLite-DB fixture test_show_backfill.py already
established.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import show_backfill, untracked_sweep


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "untracked_sweep_test.db"
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


_ITEM_A = {
    "service": "sonarr",
    "title": "Show A",
    "external_id": 111,
    "path": "/data/anime/Show A",
    "tracking_space": "anime",
    "media_shape": "episodic",
}
_ITEM_B = {
    "service": "anilist",
    "title": "Show B",
    "external_id": 222,
    "path": None,
    "tracking_space": "anime",
    "media_shape": "movie",
}

_ALL_REPORTED = {"sonarr", "radarr", "anilist"}


def _stub_preview(monkeypatch, items, reported_services=_ALL_REPORTED):
    monkeypatch.setattr(
        show_backfill,
        "preview_backfill_with_status",
        lambda c: {"items": items, "reported_services": set(reported_services)},
    )


def _findings(conn):
    return conn.execute(
        "SELECT * FROM untracked_show_finding ORDER BY service, external_id"
    ).fetchall()


def test_sweep_inserts_new_findings(conn, monkeypatch):
    _stub_preview(monkeypatch, [_ITEM_A, _ITEM_B])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 2, "new_findings": 2, "resolved_findings": 0}

    rows = _findings(conn)
    assert len(rows) == 2
    assert rows[1]["service"] == "sonarr"
    assert rows[1]["external_id"] == "111"
    assert rows[1]["title"] == "Show A"
    assert rows[1]["path"] == "/data/anime/Show A"
    assert rows[1]["tracking_space"] == "anime"
    assert rows[1]["media_shape"] == "episodic"
    assert rows[1]["first_seen_at"] == rows[1]["last_seen_at"]
    assert rows[1]["id"].startswith("u-")

    assert rows[0]["service"] == "anilist"
    assert rows[0]["path"] is None  # never had one — AniList-sourced


def test_sweep_never_creates_a_show(conn, monkeypatch):
    _stub_preview(monkeypatch, [_ITEM_A])
    untracked_sweep.sweep_untracked_shows(conn)
    assert conn.execute("SELECT COUNT(*) FROM show").fetchone()[0] == 0


def test_sweep_is_a_clean_no_op_on_an_unchanged_rerun(conn, monkeypatch):
    _stub_preview(monkeypatch, [_ITEM_A])
    untracked_sweep.sweep_untracked_shows(conn)
    first_seen = _findings(conn)[0]["first_seen_at"]

    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 0}
    rows = _findings(conn)
    assert len(rows) == 1  # not duplicated
    assert rows[0]["first_seen_at"] == first_seen  # preserved, not reset


def test_sweep_refreshes_a_changed_field_on_a_still_current_finding(conn, monkeypatch):
    _stub_preview(monkeypatch, [_ITEM_A])
    untracked_sweep.sweep_untracked_shows(conn)

    renamed = {**_ITEM_A, "title": "Show A (renamed in Sonarr)"}
    _stub_preview(monkeypatch, [renamed])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 0}

    rows = _findings(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Show A (renamed in Sonarr)"


def test_sweep_prunes_a_finding_no_longer_present(conn, monkeypatch):
    _stub_preview(monkeypatch, [_ITEM_A, _ITEM_B])
    untracked_sweep.sweep_untracked_shows(conn)

    # Show A got tracked some other way (or genuinely disappeared) — its
    # own source (sonarr) still reported successfully this pass, so its
    # absence is trustworthy; only B is still untracked next sweep.
    _stub_preview(monkeypatch, [_ITEM_B])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 1}

    rows = _findings(conn)
    assert len(rows) == 1
    assert rows[0]["service"] == "anilist"


def test_sweep_is_empty_with_nothing_untracked(conn, monkeypatch):
    _stub_preview(monkeypatch, [])
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 0, "new_findings": 0, "resolved_findings": 0}
    assert _findings(conn) == []


# --- per-source pruning gate (B.11e follow-up, real bug found in review) -----


def test_sweep_does_not_prune_a_finding_from_a_source_that_failed_to_report(conn, monkeypatch):
    """The actual bug: a transient Sonarr outage during one sweep used to
    delete every real Sonarr finding as falsely "resolved" — because a
    connection failure and "genuinely nothing untracked" both surfaced
    as the same empty contribution to the combined list. Show A's own
    source (sonarr) failing to report this pass must leave it alone,
    even though it's absent from `current`."""
    _stub_preview(monkeypatch, [_ITEM_A, _ITEM_B])
    untracked_sweep.sweep_untracked_shows(conn)
    first_seen = next(r for r in _findings(conn) if r["service"] == "sonarr")["first_seen_at"]

    # Sonarr is down this pass: absent from `items`, and NOT in
    # reported_services. AniList still reported fine and found nothing
    # new beyond B.
    _stub_preview(monkeypatch, [_ITEM_B], reported_services={"anilist", "radarr"})
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 0}

    rows = _findings(conn)
    assert {r["service"] for r in rows} == {"sonarr", "anilist"}  # A survives
    sonarr_row = next(r for r in rows if r["service"] == "sonarr")
    assert sonarr_row["first_seen_at"] == first_seen  # untouched, not refreshed either


def test_sweep_prunes_once_the_failed_source_reports_again(conn, monkeypatch):
    """The other half: once Sonarr genuinely reports (successfully) that
    Show A is no longer untracked, pruning resumes for that source."""
    _stub_preview(monkeypatch, [_ITEM_A, _ITEM_B])
    untracked_sweep.sweep_untracked_shows(conn)

    _stub_preview(monkeypatch, [_ITEM_B], reported_services={"anilist", "radarr"})
    untracked_sweep.sweep_untracked_shows(conn)  # Sonarr down — A survives

    _stub_preview(monkeypatch, [_ITEM_B])  # Sonarr back, genuinely nothing there now
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 1, "new_findings": 0, "resolved_findings": 1}
    assert {r["service"] for r in _findings(conn)} == {"anilist"}


def test_sweep_does_not_prune_anything_when_no_source_reported(conn, monkeypatch):
    _stub_preview(monkeypatch, [_ITEM_A, _ITEM_B])
    untracked_sweep.sweep_untracked_shows(conn)

    _stub_preview(monkeypatch, [], reported_services=set())
    result = untracked_sweep.sweep_untracked_shows(conn)
    assert result == {"found": 0, "new_findings": 0, "resolved_findings": 0}
    assert len(_findings(conn)) == 2  # both survive — nothing genuinely confirmed absent
