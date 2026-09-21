"""ops.scheduler — B.1 (daily metadata refresh), B.2 (Fribb
reconciliation, weekly + monthly tiers), B.3 (file availability, one
dynamic-interval loop), B.14 (cross-service show-merge sweep, rides
the monthly tier). Each run_*_once() is the real unit under test;
a fake LcarsClient stand-in (not the real httpx-backed one) keeps
these tests focused on the scheduler's own looping/error-isolation
logic, already separately covered by test_ops_lcars_client.py for the
transport layer.
"""

import pytest

from ops.lcars_client import LcarsError
from ops.scheduler import (
    _availability_loop,
    _loop,
    run_anilist_activity_once,
    run_animeschedule_once,
    run_availability_once,
    run_catalog_presence_once,
    run_daily_and_weekly_once,
    run_episode_movie_link_reconciliation_once,
    run_forever,
    run_local_presence_once,
    run_mal_token_refresh_once,
    run_monthly_once,
    run_once,
    run_season_reconciliation_once,
    run_show_merge_once,
    run_tvdb_backfill_once,
    run_untracked_shows_once,
    run_weekly_once,
)


def _season(season_id: str, show_id: str, season_number: int = 1) -> dict:
    return {"id": season_id, "seasonNumber": season_number, "show": {"id": show_id}}


class _FakeClient:
    def __init__(
        self,
        due_shows: list[dict] | None = None,
        due_seasons: list[dict] | None = None,
        all_seasons_: list[dict] | None = None,
        fail_show_ids: set[str] | None = None,
        fail_season_ids: set[str] | None = None,
        availability_result: dict | None = None,
        recommended_interval: int = 3600,
        animeschedule_result: dict | None = None,
        local_presence_result: dict | None = None,
        catalog_presence_result: dict | None = None,
        episode_movie_link_result: dict | None = None,
        mal_token_refresh_result: dict | None = None,
        untracked_shows_result: dict | None = None,
        show_merge_result: dict | None = None,
        anilist_activity_result: dict | None = None,
        tvdb_backfill_result: dict | None = None,
        season_subdivision_result: dict | None = None,
        score_sync_result: dict | None = None,
        identity_mismatch_result: dict | None = None,
        reconcile_arr_state_result: dict | None = None,
    ) -> None:
        self._due_shows = due_shows or []
        self._due_seasons = due_seasons or []
        self._all_seasons = all_seasons_ or []
        self._fail_show_ids = fail_show_ids or set()
        self._fail_season_ids = fail_season_ids or set()
        self._availability_result = availability_result or {
            "episodesUpdated": 0,
            "showsUpdated": 0,
        }
        self._recommended_interval = recommended_interval
        self._animeschedule_result = animeschedule_result or {
            "episodesUpdated": 0,
            "flagged": 0,
        }
        self._local_presence_result = local_presence_result or {"showsUpdated": 0}
        self._catalog_presence_result = catalog_presence_result or {"showsUpdated": 0}
        self._episode_movie_link_result = episode_movie_link_result or {
            "matched": 0,
            "flagged": 0,
            "unmatched": 0,
            "availabilitySynced": 0,
        }
        self._mal_token_refresh_result = mal_token_refresh_result or {"refreshed": False}
        self._untracked_shows_result = untracked_shows_result or {
            "found": 0,
            "newFindings": 0,
            "resolvedFindings": 0,
        }
        self._show_merge_result = show_merge_result or {"candidatesFound": 0, "reviewsOpened": 0}
        self._anilist_activity_result = anilist_activity_result or {
            "activitiesSeen": 0,
            "reconcileResult": None,
        }
        self._tvdb_backfill_result = tvdb_backfill_result or {"showsUpdated": 0}
        self._season_subdivision_result = season_subdivision_result or {
            "checked": 0,
            "flagged": 0,
        }
        self._score_sync_result = score_sync_result or {
            "anilistChecked": 0,
            "anilistFlagged": 0,
        }
        self._identity_mismatch_result = identity_mismatch_result or {
            "checked": 0,
            "flagged": 0,
        }
        self._reconcile_arr_state_result = reconcile_arr_state_result or {
            "episodesCorrected": 0,
            "showsCorrected": 0,
            "showsCreated": 0,
            "showsCreateFailed": 0,
            "pausedShowIds": [],
            "resumedShowIds": [],
        }
        self.refreshed: list[str] = []
        self.reconciled: list[tuple[str, int]] = []

    async def due_for_metadata_refresh(self) -> list[dict]:
        return self._due_shows

    async def refresh_show_metadata(self, show_id: str) -> dict:
        if show_id in self._fail_show_ids:
            raise LcarsError(f"boom: {show_id}")
        self.refreshed.append(show_id)
        return {"id": show_id}

    async def due_for_season_reconciliation(self) -> list[dict]:
        return self._due_seasons

    async def all_seasons(self) -> list[dict]:
        return self._all_seasons

    async def reconcile_season_mapping(self, show_id: str, season_number: int) -> dict:
        season_id = next(
            s["id"]
            for s in (self._due_seasons + self._all_seasons)
            if s["show"]["id"] == show_id and s["seasonNumber"] == season_number
        )
        if season_id in self._fail_season_ids:
            raise LcarsError(f"boom: {season_id}")
        self.reconciled.append((show_id, season_number))
        return {"id": season_id}

    async def poll_file_availability(self) -> dict:
        return self._availability_result

    async def reconcile_arr_state(self) -> dict:
        return self._reconcile_arr_state_result

    async def recommended_availability_poll_interval_seconds(self) -> int:
        return self._recommended_interval

    async def poll_anime_schedule(self) -> dict:
        return self._animeschedule_result

    async def poll_local_service_presence(self) -> dict:
        return self._local_presence_result

    async def poll_catalog_service_presence(self) -> dict:
        return self._catalog_presence_result

    async def reconcile_episode_movie_links(self) -> dict:
        return self._episode_movie_link_result

    async def refresh_mal_token_if_due(self) -> dict:
        return self._mal_token_refresh_result

    async def poll_untracked_shows(self) -> dict:
        return self._untracked_shows_result

    async def poll_show_merges(self) -> dict:
        return self._show_merge_result

    async def poll_anilist_activity(self) -> dict:
        return self._anilist_activity_result

    async def backfill_tvdb_ids(self) -> dict:
        return self._tvdb_backfill_result

    async def poll_season_subdivision(self) -> dict:
        return self._season_subdivision_result

    async def poll_score_sync(self) -> dict:
        return self._score_sync_result

    async def poll_identity_mismatch(self) -> dict:
        return self._identity_mismatch_result


# --- run_once (B.1) ---------------------------------------------------------


async def test_run_once_refreshes_every_due_show():
    client = _FakeClient(due_shows=[{"id": "s-a"}, {"id": "s-b"}])
    count = await run_once(client)
    assert count == 2
    assert client.refreshed == ["s-a", "s-b"]


async def test_run_once_is_a_clean_no_op_with_nothing_due():
    client = _FakeClient()
    assert await run_once(client) == 0
    assert client.refreshed == []


async def test_run_once_a_single_shows_failure_does_not_stop_the_rest():
    client = _FakeClient(
        due_shows=[{"id": "s-a"}, {"id": "s-b"}, {"id": "s-c"}], fail_show_ids={"s-b"}
    )
    count = await run_once(client)
    assert count == 2  # s-a and s-c succeeded, s-b's failure was caught
    assert client.refreshed == ["s-a", "s-c"]


async def test_run_once_raises_if_the_due_query_itself_fails():
    class _BrokenClient(_FakeClient):
        async def due_for_metadata_refresh(self):
            raise LcarsError("dueForMetadataRefresh unreachable")

    client = _BrokenClient()
    with pytest.raises(LcarsError):
        await run_once(client)


# --- run_weekly_once (B.2, weekly tier) -------------------------------------


async def test_run_weekly_once_reconciles_every_due_season():
    client = _FakeClient(due_seasons=[_season("z-a", "s-a"), _season("z-b", "s-b", 2)])
    count = await run_weekly_once(client)
    assert count == 2
    assert client.reconciled == [("s-a", 1), ("s-b", 2)]


async def test_run_weekly_once_is_a_clean_no_op_with_nothing_due():
    client = _FakeClient()
    assert await run_weekly_once(client) == 0
    assert client.reconciled == []


async def test_run_weekly_once_a_single_seasons_failure_does_not_stop_the_rest():
    client = _FakeClient(
        due_seasons=[_season("z-a", "s-a"), _season("z-b", "s-b"), _season("z-c", "s-c")],
        fail_season_ids={"z-b"},
    )
    count = await run_weekly_once(client)
    assert count == 2
    assert client.reconciled == [("s-a", 1), ("s-c", 1)]


# --- run_season_reconciliation_once (B.2, monthly tier's own reconciliation half) --


async def test_run_season_reconciliation_once_reconciles_every_season_unconditionally():
    client = _FakeClient(
        all_seasons_=[_season("z-a", "s-a"), _season("z-b", "s-a", 2), _season("z-c", "s-c")]
    )
    count = await run_season_reconciliation_once(client)
    assert count == 3
    assert client.reconciled == [("s-a", 1), ("s-a", 2), ("s-c", 1)]


async def test_run_season_reconciliation_once_a_single_seasons_failure_does_not_stop_the_rest():
    client = _FakeClient(
        all_seasons_=[_season("z-a", "s-a"), _season("z-b", "s-b")], fail_season_ids={"z-a"}
    )
    count = await run_season_reconciliation_once(client)
    assert count == 1
    assert client.reconciled == [("s-b", 1)]


# --- run_catalog_presence_once (B.7, rides the same monthly tier) -----------


async def test_run_catalog_presence_once_returns_shows_updated():
    client = _FakeClient(catalog_presence_result={"showsUpdated": 4})
    assert await run_catalog_presence_once(client) == 4


async def test_run_catalog_presence_once_is_zero_with_nothing_updated():
    client = _FakeClient()
    assert await run_catalog_presence_once(client) == 0


# --- run_show_merge_once (B.14, rides the monthly tier) ---------------------


async def test_run_show_merge_once_returns_reviews_opened_count():
    client = _FakeClient(show_merge_result={"candidatesFound": 3, "reviewsOpened": 2})
    assert await run_show_merge_once(client) == 2


async def test_run_show_merge_once_is_zero_with_nothing_to_review():
    client = _FakeClient()
    assert await run_show_merge_once(client) == 0


# --- run_anilist_activity_once (B.5.3, its own fourth loop) -----------------


async def test_run_anilist_activity_once_returns_activities_seen_count():
    client = _FakeClient(
        anilist_activity_result={
            "activitiesSeen": 3,
            "reconcileResult": {
                "seasonsChecked": 40,
                "notMatchedOnAnilist": 0,
                "showsStatusUpdated": 1,
                "episodesBackfilled": 2,
                "ambiguousAnilistIdConflicts": 0,
            },
        }
    )
    assert await run_anilist_activity_once(client) == 3


async def test_run_anilist_activity_once_is_zero_on_a_quiet_poll():
    client = _FakeClient()
    assert await run_anilist_activity_once(client) == 0


async def test_run_anilist_activity_once_logs_the_reconcile_breakdown_when_one_ran(caplog):
    client = _FakeClient(
        anilist_activity_result={
            "activitiesSeen": 3,
            "reconcileResult": {
                "seasonsChecked": 40,
                "notMatchedOnAnilist": 0,
                "showsStatusUpdated": 1,
                "episodesBackfilled": 2,
                "ambiguousAnilistIdConflicts": 0,
            },
        }
    )
    with caplog.at_level("INFO", logger="ops.scheduler"):
        await run_anilist_activity_once(client)
    assert any("reconcile ran" in r.message for r in caplog.records)
    assert any("episodesBackfilled=2" in r.message for r in caplog.records)


async def test_run_anilist_activity_once_logs_nothing_extra_on_a_quiet_poll(caplog):
    client = _FakeClient()
    with caplog.at_level("INFO", logger="ops.scheduler"):
        await run_anilist_activity_once(client)
    assert not any("reconcile ran" in r.message for r in caplog.records)


# --- run_monthly_once (the unit run_forever's monthly loop calls) -----------


async def test_run_monthly_once_sums_reconciliation_catalog_presence_and_show_merge():
    client = _FakeClient(
        all_seasons_=[_season("z-a", "s-a")],
        catalog_presence_result={"showsUpdated": 2},
        show_merge_result={"candidatesFound": 5, "reviewsOpened": 1},
    )
    count = await run_monthly_once(client)
    assert count == 4
    assert client.reconciled == [("s-a", 1)]


# --- run_local_presence_once (B.7, rides the hourly tick) -------------------


async def test_run_local_presence_once_returns_shows_updated():
    client = _FakeClient(local_presence_result={"showsUpdated": 5})
    assert await run_local_presence_once(client) == 5


async def test_run_local_presence_once_is_zero_with_nothing_updated():
    client = _FakeClient()
    assert await run_local_presence_once(client) == 0


# --- run_availability_once (B.3) --------------------------------------------


async def test_run_availability_once_sums_episodes_and_shows_updated():
    client = _FakeClient(availability_result={"episodesUpdated": 3, "showsUpdated": 2})
    assert await run_availability_once(client) == 5


async def test_run_availability_once_is_zero_with_nothing_updated():
    client = _FakeClient()
    assert await run_availability_once(client) == 0


async def test_run_availability_once_folds_in_reconcile_arr_state():
    client = _FakeClient(
        availability_result={"episodesUpdated": 3, "showsUpdated": 2},
        reconcile_arr_state_result={
            "episodesCorrected": 1,
            "showsCorrected": 1,
            "showsCreated": 2,
            "showsCreateFailed": 0,
            "pausedShowIds": ["s-1"],
            "resumedShowIds": ["s-2", "s-3"],
        },
    )
    # availability (3+2) + reconcile (created 2 + paused 1 + resumed 2)
    assert await run_availability_once(client) == 10


async def test_run_availability_once_survives_a_broken_reconcile_call():
    class _BrokenReconcileClient(_FakeClient):
        async def reconcile_arr_state(self):
            raise LcarsError("boom")

    client = _BrokenReconcileClient(availability_result={"episodesUpdated": 3, "showsUpdated": 2})
    # reconcile failing must not swallow the availability count that already succeeded
    assert await run_availability_once(client) == 5


# --- run_animeschedule_once (B.5) --------------------------------------------


async def test_run_animeschedule_once_sums_updated_and_flagged():
    client = _FakeClient(animeschedule_result={"episodesUpdated": 3, "flagged": 2})
    assert await run_animeschedule_once(client) == 5


async def test_run_animeschedule_once_is_zero_with_nothing_updated():
    client = _FakeClient()
    assert await run_animeschedule_once(client) == 0


# --- _availability_loop (B.3's own dynamic-interval loop) -------------------


async def test_availability_loop_sleeps_the_recommended_interval(monkeypatch):
    client = _FakeClient(recommended_interval=300)
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit  # stop after one tick

    monkeypatch.setattr("ops.scheduler.asyncio.sleep", fake_sleep)
    with pytest.raises(SystemExit):
        await _availability_loop(client)
    assert sleep_calls == [300]


async def test_availability_loop_survives_a_sweep_failure_and_still_checks_interval(monkeypatch):
    class _BrokenClient(_FakeClient):
        async def poll_file_availability(self):
            raise RuntimeError("totally unexpected")

    client = _BrokenClient(recommended_interval=900)
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit

    monkeypatch.setattr("ops.scheduler.asyncio.sleep", fake_sleep)
    with pytest.raises(SystemExit):
        await _availability_loop(client)
    # The sweep failure didn't stop the loop from reaching the interval check.
    assert sleep_calls == [900]


async def test_availability_loop_falls_back_to_baseline_if_the_interval_check_itself_fails(
    monkeypatch,
):
    class _BrokenIntervalClient(_FakeClient):
        async def recommended_availability_poll_interval_seconds(self):
            raise RuntimeError("totally unexpected")

    client = _BrokenIntervalClient()
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise SystemExit

    monkeypatch.setattr("ops.scheduler.asyncio.sleep", fake_sleep)
    with pytest.raises(SystemExit):
        await _availability_loop(client)
    assert sleep_calls == [3600]


# --- run_daily_and_weekly_once (the unit run_forever's hourly loop calls) ---


async def test_run_daily_and_weekly_once_sums_all_twelve_tiers():
    client = _FakeClient(
        due_shows=[{"id": "s-a"}],
        due_seasons=[_season("z-a", "s-b")],
        animeschedule_result={"episodesUpdated": 1, "flagged": 1},
        local_presence_result={"showsUpdated": 1},
        episode_movie_link_result={
            "matched": 1,
            "flagged": 0,
            "unmatched": 0,
            "availabilitySynced": 0,
        },
        mal_token_refresh_result={"refreshed": True},
        untracked_shows_result={"found": 5, "newFindings": 1, "resolvedFindings": 1},
        tvdb_backfill_result={"showsUpdated": 1},
        season_subdivision_result={"checked": 3, "flagged": 1},
        score_sync_result={"anilistChecked": 5, "anilistFlagged": 1},
        identity_mismatch_result={"checked": 4, "flagged": 1},
    )
    count = await run_daily_and_weekly_once(client)
    # 1 (show refresh) + 1 (season reconcile) + 2 (animeschedule) + 1 (local
    # presence) + 1 (episode movie links) + 1 (mal refresh) + 2 (untracked) +
    # 1 (tvdb backfill) + 4 (season subdivision checked+flagged) +
    # 6 (score_sync checked+flagged) + 5 (identity_mismatch checked+flagged) = 25
    # identity_mismatch added 2026-09-21 — found via the real Tantei/
    # Milky-Holmes incident, see identity_mismatch.py's own module docstring.
    assert count == 25
    assert client.refreshed == ["s-a"]
    assert client.reconciled == [("s-b", 1)]


# --- run_tvdb_backfill_once (2026-08-18) --------------------------------------


async def test_run_tvdb_backfill_once_returns_the_shows_updated_count():
    client = _FakeClient(tvdb_backfill_result={"showsUpdated": 3})
    assert await run_tvdb_backfill_once(client) == 3


async def test_run_tvdb_backfill_once_returns_zero_on_a_no_op():
    client = _FakeClient()
    assert await run_tvdb_backfill_once(client) == 0


# --- run_mal_token_refresh_once (B.10) ----------------------------------------


async def test_run_mal_token_refresh_once_returns_one_when_a_refresh_happened():
    client = _FakeClient(mal_token_refresh_result={"refreshed": True})
    assert await run_mal_token_refresh_once(client) == 1


async def test_run_mal_token_refresh_once_returns_zero_on_a_no_op():
    client = _FakeClient(mal_token_refresh_result={"refreshed": False})
    assert await run_mal_token_refresh_once(client) == 0


# --- run_untracked_shows_once (B.11e) -----------------------------------------


async def test_run_untracked_shows_once_sums_new_and_resolved_not_found():
    client = _FakeClient(
        untracked_shows_result={"found": 12, "newFindings": 2, "resolvedFindings": 3}
    )
    # "found" is the total current list, not a change — excluded from the
    # count, matching every other tier's "count real changes" convention.
    assert await run_untracked_shows_once(client) == 5


async def test_run_untracked_shows_once_returns_zero_on_a_clean_sweep():
    client = _FakeClient(
        untracked_shows_result={"found": 0, "newFindings": 0, "resolvedFindings": 0}
    )
    assert await run_untracked_shows_once(client) == 0


# --- run_episode_movie_link_reconciliation_once (B.8b) -----------------------


async def test_run_episode_movie_link_reconciliation_once_sums_all_four_counts():
    client = _FakeClient(
        episode_movie_link_result={
            "matched": 1,
            "flagged": 2,
            "unmatched": 3,
            "availabilitySynced": 4,
        }
    )
    count = await run_episode_movie_link_reconciliation_once(client)
    assert count == 10


# --- _loop (the shared per-tier while-loop primitive) -----------------------


async def test_loop_survives_a_non_lcars_error_and_keeps_ticking(monkeypatch):
    """A genuinely unanticipated exception (not just LcarsError) must
    still be caught — this loop is what an unattended daemon actually
    runs, so a bare uncaught exception here would silently kill the
    whole process rather than logging and retrying next interval."""

    async def broken_coro_fn(client):
        raise RuntimeError("totally unexpected — not an LcarsError")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise SystemExit  # stop the infinite loop after 2 ticks

    monkeypatch.setattr("ops.scheduler.asyncio.sleep", fake_sleep)
    with pytest.raises(SystemExit):
        await _loop(broken_coro_fn, _FakeClient(), interval_seconds=1, label="test")
    # Reached a second tick — the first RuntimeError was caught, logged, and
    # didn't stop the loop.
    assert len(sleep_calls) == 2


# --- run_forever (wiring only — each loop's own behavior is covered above) --


async def test_run_forever_wires_up_all_six_loops(monkeypatch):
    calls = []

    async def fake_loop(coro_fn, client, interval_seconds, label):
        calls.append((coro_fn.__name__, interval_seconds, label))

    availability_calls = []

    async def fake_availability_loop(client):
        availability_calls.append(client)

    monkeypatch.setattr("ops.scheduler._loop", fake_loop)
    monkeypatch.setattr("ops.scheduler._availability_loop", fake_availability_loop)
    client = _FakeClient()
    await run_forever(
        client,
        interval_seconds=3600,
        monthly_interval_seconds=2592000,
        anilist_activity_interval_seconds=240,
        mal_reconcile_interval_seconds=3600,
        memory_alpha_interval_seconds=1200,
    )
    assert set(calls) == {
        (
            "run_daily_and_weekly_once",
            3600,
            "daily+weekly+animeschedule+local_presence+episode_movie_links"
            "+mal_token_refresh+untracked_shows+tvdb_backfill",
        ),
        ("run_monthly_once", 2592000, "monthly+catalog_presence+show_merge"),
        ("run_anilist_activity_once", 240, "anilist_activity"),
        ("run_mal_reconcile_once", 3600, "mal_reconcile"),
        ("run_memory_alpha_once", 1200, "memory_alpha"),
    }
    assert availability_calls == [client]


async def test_run_forever_defaults_anilist_activity_interval_to_240s(monkeypatch):
    calls = []

    async def fake_loop(coro_fn, client, interval_seconds, label):
        calls.append((coro_fn.__name__, interval_seconds, label))

    async def fake_availability_loop(client):
        pass

    monkeypatch.setattr("ops.scheduler._loop", fake_loop)
    monkeypatch.setattr("ops.scheduler._availability_loop", fake_availability_loop)
    client = _FakeClient()
    await run_forever(client, interval_seconds=3600, monthly_interval_seconds=2592000)
    assert ("run_anilist_activity_once", 240, "anilist_activity") in calls
