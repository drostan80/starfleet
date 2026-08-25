"""lcars/events.py — the in-process event bus backing outbound GraphQL
subscriptions, 2026-08-25 ("webhook push to clients" design). Unit-level
only (no WS transport involved) — see test_subscriptions.py for the
real WS end-to-end coverage (auth, protocol, resolver wiring)."""

import asyncio

import pytest

from lcars import events


@pytest.fixture(autouse=True)
def _reset_subscribers():
    """Module-level `_subscribers` dict is shared process-wide (module
    docstring) — clear it before and after every test so one test's
    leftover queue can't leak into the next."""
    events._subscribers.clear()
    yield
    events._subscribers.clear()


async def test_subscribe_yields_a_published_event():
    gen = events.subscribe("topic-a")
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)  # let the generator register its queue before publishing
    events.publish("topic-a", "payload-1")
    assert await task == "payload-1"


async def test_publish_with_no_subscribers_is_a_silent_no_op():
    events.publish("nobody-listening", "payload")  # must not raise


async def test_publish_only_reaches_subscribers_of_the_matching_topic():
    gen_a = events.subscribe("topic-a")
    gen_b = events.subscribe("topic-b")
    task_a = asyncio.ensure_future(gen_a.__anext__())
    task_b = asyncio.ensure_future(gen_b.__anext__())
    await asyncio.sleep(0)
    events.publish("topic-a", "for-a-only")
    await asyncio.sleep(0)
    assert task_a.done()
    assert not task_b.done()
    assert await task_a == "for-a-only"
    task_b.cancel()


async def test_publish_reaches_every_concurrent_subscriber_of_the_same_topic():
    gen_1 = events.subscribe("topic-a")
    gen_2 = events.subscribe("topic-a")
    task_1 = asyncio.ensure_future(gen_1.__anext__())
    task_2 = asyncio.ensure_future(gen_2.__anext__())
    await asyncio.sleep(0)
    events.publish("topic-a", "broadcast")
    assert await task_1 == "broadcast"
    assert await task_2 == "broadcast"


async def test_a_full_subscriber_queue_drops_the_new_event_rather_than_blocking():
    gen = events.subscribe("topic-a")
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    for i in range(events._MAX_QUEUE_SIZE + 5):
        events.publish("topic-a", f"event-{i}")  # must not raise/block even once full
    assert await task == "event-0"  # nothing dropped displaced what was already queued


async def test_subscriber_queue_is_deregistered_on_generator_close():
    gen = events.subscribe("topic-a")
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    assert len(events._subscribers["topic-a"]) == 1
    task.cancel()  # the generator is suspended inside this task's own await
    with pytest.raises(asyncio.CancelledError):
        await task
    await gen.aclose()
    assert len(events._subscribers["topic-a"]) == 0
