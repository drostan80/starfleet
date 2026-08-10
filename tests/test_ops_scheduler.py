"""ops.scheduler — B.1 (daily metadata refresh), B.2 (Fribb
reconciliation, weekly + monthly tiers), B.3 (file availability, one
dynamic-interval loop). Each run_*_once() is the real unit under test;
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


# --- run_monthly_once (the unit run_forever's monthly loop calls) -----------


async def test_run_monthly_once_sums_reconciliation_and_catalog_presence():
    client = _FakeClient(
        all_seasons_=[_season("z-a", "s-a")], catalog_presence_result={"showsUpdated": 2}
    )
    count = await run_monthly_once(client)
    assert count == 3
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


async def test_run_daily_and_weekly_once_sums_all_six_tiers():
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
    )
    count = await run_daily_and_weekly_once(client)
    assert count == 7
    assert client.refreshed == ["s-a"]
    assert client.reconciled == [("s-b", 1)]


# --- run_mal_token_refresh_once (B.10) ----------------------------------------


async def test_run_mal_token_refresh_once_returns_one_when_a_refresh_happened():
    client = _FakeClient(mal_token_refresh_result={"refreshed": True})
    assert await run_mal_token_refresh_once(client) == 1


async def test_run_mal_token_refresh_once_returns_zero_on_a_no_op():
    client = _FakeClient(mal_token_refresh_result={"refreshed": False})
    assert await run_mal_token_refresh_once(client) == 0


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


async def test_run_forever_wires_up_all_three_loops(monkeypatch):
    calls = []

    async def fake_loop(coro_fn, client, interval_seconds, label):
        calls.append((coro_fn.__name__, interval_seconds, label))

    availability_calls = []

    async def fake_availability_loop(client):
        availability_calls.append(client)

    monkeypatch.setattr("ops.scheduler._loop", fake_loop)
    monkeypatch.setattr("ops.scheduler._availability_loop", fake_availability_loop)
    client = _FakeClient()
    await run_forever(client, interval_seconds=3600, monthly_interval_seconds=2592000)
    assert set(calls) == {
        (
            "run_daily_and_weekly_once",
            3600,
            "daily+weekly+animeschedule+local_presence+episode_movie_links+mal_token_refresh",
        ),
        ("run_monthly_once", 2592000, "monthly+catalog_presence"),
    }
    assert availability_calls == [client]
