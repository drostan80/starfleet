"""In-process event bus backing LCARS's outbound GraphQL subscriptions —
NEXT_UP.md, 2026-08-25 ("webhook push to clients" design discussion).

**Why a subscription, not an actual outbound webhook, was the design
choice made here (not this module's call to relitigate — see
NEXT_UP.md's own write-up for the full comparison)**: a true webhook
would require every client (Data first, `ResolvedByClient`'s own
`holodeck`/`captains_log` values name the other two this schema already
anticipates) to run its own HTTP listener and be reachable at a known
address — real new attack surface on what's otherwise an outbound-only
personal app. A subscription flips the direction: the client opens one
long-lived connection *out* to LCARS, same as every other call it
already makes, and LCARS pushes events down that existing connection.
Nothing for a client to bind or expose.

**Deliberately just an in-memory fan-out, no persistence** — "fine to
miss" was an explicit, discussed decision, not an oversight. An event
published while nobody is subscribed, or while a subscriber's own queue
is already full (it's fallen behind), is simply dropped. Every
downstream consumer already has its own independent source of truth
(Data's periodic calendar/show refresh, `pollFileAvailability` server-
side) that a missed event doesn't invalidate — this exists purely as a
latency optimization layered on top of those, never the only path by
which state reaches a client. A dropped event is not a bug report.

**Topics are plain strings, not an enum** — this module has no opinion
on what topics exist; that's entirely up to whoever calls `publish`/
`subscribe` (resolvers.py's own `SubscriptionType` wiring, currently).
Adding a new pushed event later (§ the "adding shows to a client's
calendar immediately" case this was scoped for from the start) is one
more `publish()` call at the point it happens, plus one more schema
field + subscription source — no change here.

**Thread/loop safety**: `asyncio.Queue` is not thread-safe across
threads, but this whole process (uvicorn, single worker — see
starfleet.yml) runs one event loop; every resolver, sync or async,
executes on that same loop/thread, so a plain synchronous `publish()`
call from an ordinary (non-async) resolver is safe exactly the way
every other shared-mutable-state access already is in this
single-threaded app.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger("lcars.events")

# topic -> set of queues, one queue per currently-connected subscriber.
# Module-level, not something callers construct — same "one shared thing
# the whole process reaches into" shape db.py's own connection already
# has, and for the same reason: every publish site and every subscriber
# needs to agree on the exact same registry.
_subscribers: dict[str, set[asyncio.Queue]] = {}

# Bounded so one subscriber that's stopped reading (a stalled/dead
# connection ariadne hasn't noticed yet) can't grow this process's
# memory without limit — see module docstring's "fine to miss" note;
# a full queue drops the newest event rather than blocking the
# publisher, which would otherwise stall the mutation/webhook/poll
# call site that triggered it.
_MAX_QUEUE_SIZE = 32


def publish(topic: str, payload) -> None:
    """Fire-and-forget. Safe to call from a plain sync resolver/helper
    (see module docstring) — never awaited, never raises on a full or
    empty subscriber set."""
    for queue in _subscribers.get(topic, ()):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(
                "events: dropped a %r event — a subscriber's queue is already full"
                " (§ fine-to-miss, not a bug)",
                topic,
            )


async def subscribe(topic: str) -> AsyncIterator:
    """Async generator — ariadne's `SubscriptionType.source` calls this
    once per connected WS client per subscribed field, and iterates it
    for the lifetime of that subscription. Registers a fresh queue on
    entry, always deregisters on exit (client disconnect, subscription
    cancelled, or the generator simply garbage-collected) — a queue left
    registered after its own client is gone would keep `publish` calls
    growing it forever."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    _subscribers.setdefault(topic, set()).add(queue)
    try:
        while True:
            yield await queue.get()
    finally:
        _subscribers[topic].discard(queue)
