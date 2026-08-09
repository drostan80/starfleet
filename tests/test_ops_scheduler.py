"""ops.scheduler — B.1. run_once() is the real unit under test; a fake
LcarsClient stand-in (not the real httpx-backed one) keeps these tests
focused on the scheduler's own looping/error-isolation logic, already
separately covered by test_ops_lcars_client.py for the transport layer.
"""

import asyncio

import pytest

from ops.lcars_client import LcarsError
from ops.scheduler import run_forever, run_once


class _FakeClient:
    def __init__(self, due: list[dict], fail_ids: set[str] | None = None) -> None:
        self._due = due
        self._fail_ids = fail_ids or set()
        self.refreshed: list[str] = []

    async def due_for_metadata_refresh(self) -> list[dict]:
        return self._due

    async def refresh_show_metadata(self, show_id: str) -> dict:
        if show_id in self._fail_ids:
            raise LcarsError(f"boom: {show_id}")
        self.refreshed.append(show_id)
        return {"id": show_id}


async def test_run_once_refreshes_every_due_show():
    client = _FakeClient([{"id": "s-a"}, {"id": "s-b"}])
    count = await run_once(client)
    assert count == 2
    assert client.refreshed == ["s-a", "s-b"]


async def test_run_once_is_a_clean_no_op_with_nothing_due():
    client = _FakeClient([])
    assert await run_once(client) == 0
    assert client.refreshed == []


async def test_run_once_a_single_shows_failure_does_not_stop_the_rest():
    client = _FakeClient([{"id": "s-a"}, {"id": "s-b"}, {"id": "s-c"}], fail_ids={"s-b"})
    count = await run_once(client)
    assert count == 2  # s-a and s-c succeeded, s-b's failure was caught
    assert client.refreshed == ["s-a", "s-c"]


async def test_run_once_raises_if_the_due_query_itself_fails():
    class _BrokenClient(_FakeClient):
        async def due_for_metadata_refresh(self):
            raise LcarsError("dueForMetadataRefresh unreachable")

    client = _BrokenClient([])
    with pytest.raises(LcarsError):
        await run_once(client)


async def test_run_forever_survives_a_non_lcars_error_and_keeps_looping(monkeypatch):
    """A genuinely unanticipated exception (not just LcarsError) must
    still be caught — run_forever is what an unattended daemon actually
    calls, so a bare uncaught exception here would silently kill the
    whole process rather than logging and retrying next interval."""

    class _BrokenClient(_FakeClient):
        async def due_for_metadata_refresh(self):
            raise RuntimeError("totally unexpected — not an LcarsError")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError  # stop the infinite loop after 2 ticks

    monkeypatch.setattr("ops.scheduler.asyncio.sleep", fake_sleep)
    client = _BrokenClient([])
    with pytest.raises(asyncio.CancelledError):
        await run_forever(client, interval_seconds=1)
    # Reached a second tick — the first RuntimeError was caught, logged, and
    # didn't stop the loop.
    assert len(sleep_calls) == 2
