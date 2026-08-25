"""Real WS end-to-end coverage for the outbound GraphQL subscriptions —
2026-08-25 ("webhook push to clients" design). Deliberately a separate
file/harness from test_server.py's own httpx.ASGITransport-based
`client` fixture: ASGITransport is HTTP-only, it can't drive a WS
handshake at all, so this uses Starlette's own TestClient (sync,
requests-based, but with real WS support) instead — the one place in
this test suite that needs it, for the one part of the app (WS) that
actually needs a live protocol exchange, not just a request/response
call.

Covers exactly what's new/risky here: the auth gap `BearerTokenMiddleware`
used to have on non-http scopes (a real find, not a hypothetical — see
its own docstring), the graphql-transport-ws handshake actually working
end to end, and each subscription field firing — delivered through the
same real mutation/webhook calls a client would actually make, not a
raw DB poke. Not a re-test of events.py's own pub/sub mechanics
(test_events.py) or of the "publish only on a genuine status change"
business logic (test_availability.py, no ASGI/threading needed for
that one) — this is specifically "does the wire actually work."
"""

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.routing import Router
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lcars import config, db, events, sonarr_client
from lcars.server import build_app

BEARER_TOKEN = "test-token-123"
SONARR_WEBHOOK_SECRET = "sonarr-secret-abc"


@pytest.fixture
def migrated_db(tmp_path) -> Path:
    # Same fixture as test_server.py's own — copied, not imported/shared
    # via conftest.py, matching this test suite's existing per-file
    # convention (test_export_import.py has its own variant too).
    db_path = tmp_path / "lcars_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True,
        capture_output=True,
    )
    return db_path


@pytest.fixture
def ws_client(migrated_db, monkeypatch):
    """Same setup as test_server.py's own `client` fixture, but wrapped
    in Starlette's sync TestClient instead of httpx.ASGITransport, since
    only TestClient can drive `websocket_connect` at all.

    That swap has a real consequence db.py's own module docstring
    predicts almost exactly: TestClient bridges sync test code to the
    async app via a background "portal" thread — every request (HTTP
    *and* WS) actually runs there, not on this fixture's own thread.
    db.py deliberately leaves sqlite3's `check_same_thread` guard on
    (its own docstring: "if some future change accidentally introduces
    threading... sqlite3 raises immediately") — calling `db.connect()`
    here, on this fixture's thread, would make every later request hit
    exactly that guard, correctly, as a *different* thread. The fix is
    the textbook one for exactly this shape: an ASGI lifespan hook, so
    `db.connect()` runs at app startup on whatever thread actually ends
    up serving requests — production's own uvicorn (single thread
    already, so this is a no-op difference there) gets the identical
    call, just relocated; only this test harness's own threading model
    makes the relocation load-bearing."""
    from lcars import anilist_client, fribb

    config.set_current(config.Config())
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: None)
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    monkeypatch.setattr(fribb, "load_dataset", lambda: [])

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        db.connect(migrated_db)
        yield
        db.close()

    app = build_app(BEARER_TOKEN, sonarr_webhook_secret=SONARR_WEBHOOK_SECRET)
    wrapped = Router(app.routes, lifespan=_lifespan)
    with TestClient(wrapped) as c:
        yield c


def _gql(client, query: str, variables: dict | None = None) -> dict:
    """Sync counterpart to test_server.py's own `gql()` — TestClient's
    `.post()` returns a plain (non-awaitable) response."""
    resp = client.post(
        "/", json={"query": query, "variables": variables or {}}, headers=_auth_headers()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "errors" not in body, body["errors"]
    return body["data"]


def _add_show(client, **overrides) -> dict:
    input_ = {
        "mediaShape": "EPISODIC",
        "trackingSpace": "ANIME",
        "titleRomaji": "Konosuba",
        "primaryTitle": "ROMAJI",
        **overrides,
    }
    data = _gql(
        client,
        "mutation($input: AddShowInput!) { addShow(input: $input) { id } }",
        {"input": input_},
    )
    return data["addShow"]


class _FakeSonarrClient:
    """Minimal copy of test_server.py's own — just the two methods
    metadata.fetch_and_populate's Sonarr path actually calls."""

    def __init__(self, series=None, episodes=None):
        self._series = series
        self._episodes = episodes or []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def series_by_tvdb_id(self, tvdb_id):
        return self._series

    def episodes(self, series_id, include_episode_file=False):
        return self._episodes


@pytest.fixture(autouse=True)
def _reset_subscribers():
    events._subscribers.clear()
    yield
    events._subscribers.clear()


def _auth_headers(token: str = BEARER_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- auth --------------------------------------------------------------


def test_websocket_without_a_bearer_token_is_refused(ws_client):
    # Real gap this build closed: BearerTokenMiddleware used to let any
    # non-"http" scope straight through unauthenticated. code=4401 is
    # this middleware's own deliberate denial code (server.py) — a
    # bare WebSocketDisconnect alone wouldn't distinguish "correctly
    # rejected" from some unrelated handshake failure.
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect("/", subprotocols=["graphql-transport-ws"]):
            pass
    assert exc_info.value.code == 4401


def test_websocket_with_the_wrong_bearer_token_is_refused(ws_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(
            "/", subprotocols=["graphql-transport-ws"], headers=_auth_headers("wrong-token")
        ):
            pass
    assert exc_info.value.code == 4401


def test_websocket_with_a_valid_bearer_token_completes_the_handshake(ws_client):
    with ws_client.websocket_connect(
        "/", subprotocols=["graphql-transport-ws"], headers=_auth_headers()
    ) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json() == {"type": "connection_ack"}


def test_disconnecting_deregisters_the_subscriber_queue(ws_client):
    """Real cleanup-on-disconnect question raised in review: does the
    server side actually notice a dropped client and deregister its
    events.py queue, or does it sit there forever, growing on every
    later publish until something eventually times it out? events.py's
    own `subscribe()` deregisters in its `finally` block on generator
    close (test_events.py covers that in isolation) — this confirms
    ariadne's own WS handler actually closes that generator when the
    underlying connection drops, not just when a caller calls
    `aclose()` directly."""
    with ws_client.websocket_connect(
        "/", subprotocols=["graphql-transport-ws"], headers=_auth_headers()
    ) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json() == {"type": "connection_ack"}
        ws.send_json(_SUBSCRIBE_AVAILABILITY)
        # No reliable "subscription is now active" ack in the protocol
        # itself — publish once and consume it as confirmation the
        # source generator has actually started (and so registered its
        # queue) before this checks anything.
        resp = ws_client.post(
            "/webhooks/sonarr",
            json={
                "eventType": "Grab",
                "series": {"id": 1, "title": "warmup", "tvdbId": 1},
                "episodes": [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}],
            },
            headers={"X-Lcars-Webhook-Secret": SONARR_WEBHOOK_SECRET},
        )
        assert resp.status_code == 200
        # (No matching episode exists, so no `next` message actually
        # arrives — the point here is only that the subscribe reached
        # the server and registered a queue, confirmed below.)
        assert len(events._subscribers.get("episode_availability_changed", ())) == 1

    assert len(events._subscribers.get("episode_availability_changed", ())) == 0


# --- episodeAvailabilityChanged -----------------------------------------

_SUBSCRIBE_AVAILABILITY = {
    "type": "subscribe",
    "id": "1",
    "payload": {
        "query": "subscription { episodeAvailabilityChanged { id availableViaSonarr } }"
    },
}


def test_episode_availability_changed_delivers_on_a_genuine_transition(ws_client, monkeypatch):
    # Real fetch, not a raw INSERT: the episode row (and the connection
    # that touches it) has to come from a call that runs on ws_client's
    # own portal thread, same as everything else here — see ws_client's
    # own docstring on why a direct db.get_connection() from this test's
    # thread isn't safe once TestClient is involved.
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    fake = _FakeSonarrClient(
        series={"id": 42},
        episodes=[{"seasonNumber": 1, "episodeNumber": 1, "airDateUtc": None, "runtime": None}],
    )
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = _add_show(ws_client, trackingSpace="TV", tvdbId=555)
    episode_id = _gql(
        ws_client,
        "query($id: ID!) { show(id: $id) { episodes { edges { node { id } } } } }",
        {"id": show["id"]},
    )["show"]["episodes"]["edges"][0]["node"]["id"]

    with ws_client.websocket_connect(
        "/", subprotocols=["graphql-transport-ws"], headers=_auth_headers()
    ) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json() == {"type": "connection_ack"}
        ws.send_json(_SUBSCRIBE_AVAILABILITY)

        # The real trigger — a Sonarr "Grab" webhook, same route
        # apply_sonarr_webhook's own tests already exercise.
        resp = ws_client.post(
            "/webhooks/sonarr",
            json={
                "eventType": "Grab",
                "series": {"id": 42, "title": "Test Show", "tvdbId": 555},
                "episodes": [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}],
            },
            headers={"X-Lcars-Webhook-Secret": SONARR_WEBHOOK_SECRET},
        )
        assert resp.status_code == 200

        message = ws.receive_json()
        assert message["type"] == "next"
        assert message["id"] == "1"
        assert message["payload"]["data"]["episodeAvailabilityChanged"] == {
            "id": episode_id,
            "availableViaSonarr": "DOWNLOADING",
        }


# --- showCreated ---------------------------------------------------------


def test_show_created_delivers_when_a_show_is_published(ws_client):
    with ws_client.websocket_connect(
        "/", subprotocols=["graphql-transport-ws"], headers=_auth_headers()
    ) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json() == {"type": "connection_ack"}
        ws.send_json(
            {
                "type": "subscribe",
                "id": "1",
                "payload": {"query": "subscription { showCreated { id displayTitle } }"},
            }
        )

        show = _add_show(ws_client, primaryTitle="ROMAJI", titleRomaji="Sub Test Show 3")

        message = ws.receive_json()
        assert message["payload"]["data"]["showCreated"] == {
            "id": show["id"],
            "displayTitle": "Sub Test Show 3",
        }
