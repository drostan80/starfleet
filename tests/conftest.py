"""Repo-wide pytest fixtures.

2026-08-13 — `anilist_client._throttle_anilist_call()` (the real
production rate-limit fix for the "22 false pending_review 'Too many
requests' entries" burst, see that module's own comment) puts a real
`time.sleep()` on the single choke point every AniList GraphQL call in
this codebase passes through. Left alone, every test that touches
`anilist_client` — directly or indirectly, e.g. via `metadata.py`/
`show_backfill.py`/`watch_reconcile.py` — would pay up to 2.1s each
once a prior test in the same process has already made one call,
since the throttle's "last call" state is module-level and persists
across tests. Autoused so no individual test file needs its own copy
of this, the same way `test_show_backfill.py`'s pre-existing per-test
`monkeypatch.setattr(show_backfill.time, "sleep", ...)` calls already
neuter that module's own, separate, real sleep — this generalizes the
same idea process-wide rather than adding it test-by-test.

B.5.3, same day — `watch_reconcile._viewer_id_cache` is the same class
of problem: module-level, deliberately persists across calls within one
real process (so a real deployment only ever fetches the viewer id
once), but that means a test that populates it would leak that value
into every later test in the same pytest process regardless of what
that later test's own `fetch_viewer_id` monkeypatch says — a real bug
caught before it could hide a wrong-viewer-id test failure, not a
hypothetical. Reset alongside the throttle for the same reason.
"""

import pytest

from lcars import anilist_client, watch_reconcile


@pytest.fixture(autouse=True)
def _no_real_anilist_throttle_sleep(monkeypatch):
    monkeypatch.setattr(anilist_client.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _reset_anilist_viewer_id_cache(monkeypatch):
    monkeypatch.setattr(watch_reconcile, "_viewer_id_cache", None)


@pytest.fixture(autouse=True)
def _no_real_anilist_release_status(monkeypatch):
    """2026-09-25 — `season_ranges.auto_season_fields` asks AniList
    whether a season has finished airing when LCARS creates a season row
    with no episodes. No test may reach the real API through that; a
    test that cares patches `fetch_media_statuses` itself."""
    monkeypatch.setattr(anilist_client, "fetch_media_statuses", lambda ids, client=None: {})
