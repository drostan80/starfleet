"""The ASGI app — Ariadne schema binding, bearer-token auth, the
X-LCARS-Client context extraction (SCOPE.md §8, §5.7 addendum), and
(B.5.1, 2026-08-13) the Sonarr/Radarr webhook routes.

`create_app()` (not a bare module-level `app`) is the real entrypoint —
deliberately, so building an app is an explicit action with its own DB
connection, not an import-time side effect that would fire on `import
lcars.server` alone (e.g. during test collection). Run via uvicorn's
factory mode: `uvicorn lcars.server:create_app --factory`.

B.5.1 — the app is a real `Router` now, not a bare `GraphQL` app wrapped
in one middleware: webhooks can't present a bearer token (Sonarr/Radarr
send their own per-service secret via a custom header instead, see
`_webhook_view` below), so `BearerTokenMiddleware` must scope to the
GraphQL mount only, not the whole app — getting this backwards is how
GraphQL would end up accidentally unauthenticated. GraphQL still answers
at exactly `/` (existing clients — Data, Ops — already POST there;
unchanged), matched last so the two `/webhooks/*` routes take priority.
"""

import hmac
import logging
from importlib import resources
from pathlib import Path

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from ariadne.asgi.handlers import GraphQLTransportWSHandler
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from lcars import availability, config, db
from lcars.resolvers import BINDABLES

logger = logging.getLogger("lcars.server")


def _load_sdl() -> str:
    return resources.files("lcars").joinpath("schema.graphql").read_text()


def build_schema():
    return make_executable_schema(_load_sdl(), *BINDABLES, convert_names_case=True)


def _context_value(request: Request, _data: dict) -> dict:
    # §5.7 addendum — required by every history/pending_review-writing
    # mutation (resolvers.require_client), not validated here: an absent
    # header is a per-mutation concern, not a whole-request auth gate.
    return {"client": request.headers.get("x-lcars-client")}


class BearerTokenMiddleware:
    """§8 — single static bearer token auth. Plain ASGI middleware, not
    GraphQL-context-based: an auth failure is a transport-level 401, not
    a GraphQL-shaped error, so it belongs in front of the GraphQL app
    entirely, not inside a resolver or context builder.

    Covers `websocket` scope too, not just `http` — real gap found while
    building the outbound-subscription mechanism (events.py, 2026-08-25,
    "webhook push to clients" design): this used to let *any* scope type
    other than `http` straight through unauthenticated, which was
    harmless while GraphQL only ever answered plain HTTP requests but
    would have left the new WS subscription endpoint wide open the
    moment `type Subscription` existed. The WS handshake itself carries
    plain HTTP headers (ASGI exposes them via `scope["headers"]` same as
    an HTTP request) — a Python client can set an `Authorization` header
    on a WS handshake same as any other request (unlike a browser's
    `WebSocket` API, which can't; not a constraint here, every current
    and anticipated client — Data/Holodeck/Captain's Log — is a Python
    process), so this reuses the exact same header/comparison rather
    than inventing a second, connection-init-payload-based scheme."""

    def __init__(self, app: ASGIApp, bearer_token: str | None) -> None:
        self._app = app
        # A `Bearer <token>` string compared whole, once, via compare_digest
        # — constant-time, and correct even when bearer_token is None (an
        # unconfigured server rejects everything rather than accepting any
        # request, which comparing against "" naively could risk).
        self._expected = f"Bearer {bearer_token}" if bearer_token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        # Headers(scope=...) rather than Request(scope, receive): Request
        # asserts scope["type"] == "http" in its own constructor (Starlette
        # internals, not documented as a public constraint) — this is the
        # one thing here that has to work for both scope types uniformly.
        presented = Headers(scope=scope).get("authorization", "")
        if self._expected is None or not hmac.compare_digest(presented, self._expected):
            if scope["type"] == "websocket":
                # PlainTextResponse is an HTTP response type, doesn't know
                # how to speak the WS ASGI sub-protocol — deny the
                # handshake outright instead (Starlette's own denial
                # shape, ASGI spec allows closing before ever accepting),
                # same "closed before it ever opens" posture a 401 gives
                # an HTTP request. 4401 is in the private-use range
                # (4000-4999) the WS spec reserves for exactly this.
                await WebSocketClose(code=4401)(scope, receive, send)
                return
            response = PlainTextResponse("Unauthorized", status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _webhook_view(header_name: str, secret: str | None, apply_fn):
    """B.5.1 — one route factory shared by `/webhooks/sonarr` and
    `/webhooks/radarr`: constant-time header-secret check (same
    `hmac.compare_digest` pattern as `BearerTokenMiddleware`, `secret is
    None` rejects everything rather than accepting any request, same
    reasoning as that class's own `_expected` — an unconfigured route
    stays closed, not open), then hands the parsed JSON body to
    `apply_fn` (`availability.apply_sonarr_webhook`/`apply_radarr_webhook`).

    A malformed body or a payload shape this doesn't recognize is a
    no-op, not a 500 — Sonarr/Radarr's own retry-on-error behavior on a
    real failure response has no benefit here (a payload we can't act on
    now won't be actionable on retry either), and `pollFileAvailability`
    remains the reconciling safety net regardless (module docstring)."""

    async def view(request: Request):
        presented = request.headers.get(header_name, "")
        if secret is None or not hmac.compare_digest(presented, secret):
            return PlainTextResponse("Unauthorized", status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            logger.warning("Webhook to %s: body was not valid JSON", request.url.path)
            return JSONResponse({"ok": True})  # see docstring — ack, don't error
        try:
            result = apply_fn(db.get_connection(), payload)
        except Exception:
            # Best-effort, matching this whole module's philosophy (A.8):
            # an unexpected payload shape must never surface as a 500 that
            # could make Sonarr/Radarr treat the connection as broken. The
            # rollback matters more than usual here: db.py hands out one
            # shared connection to the whole app, so an uncommitted write
            # left dangling by a mid-loop exception would otherwise ride
            # along on the next unrelated conn.commit() anywhere else in
            # the process.
            db.get_connection().rollback()
            logger.exception("Webhook to %s: failed to apply", request.url.path)
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": True, **result})

    return view


def build_app(
    bearer_token: str | None,
    sonarr_webhook_secret: str | None = None,
    radarr_webhook_secret: str | None = None,
    web_root: Path | None = None,
) -> ASGIApp:
    schema = build_schema()
    # websocket_handler: explicit, not the default. Ariadne's own default
    # is GraphQLWSHandler (the older, deprecated "graphql-ws" subprotocol
    # — Apollo itself has moved off it for years) unless told otherwise;
    # GraphQLTransportWSHandler speaks the current "graphql-transport-ws"
    # subprotocol instead. Only matters from schema.graphql's `type
    # Subscription` onward (2026-08-25, "webhook push to clients" design)
    # — no subscription fields existed before that to make this decision
    # visible at all.
    graphql_app = GraphQL(
        schema, context_value=_context_value, websocket_handler=GraphQLTransportWSHandler()
    )
    protected_graphql = BearerTokenMiddleware(graphql_app, bearer_token)
    routes = [
        Route(
            "/webhooks/sonarr",
            _webhook_view(
                "x-lcars-webhook-secret", sonarr_webhook_secret, availability.apply_sonarr_webhook
            ),
            methods=["POST"],
        ),
        Route(
            "/webhooks/radarr",
            _webhook_view(
                "x-lcars-webhook-secret", radarr_webhook_secret, availability.apply_radarr_webhook
            ),
            methods=["POST"],
        ),
    ]

    # Static web client — mounted at /ui if web_root is configured and exists.
    # Same-origin as the GraphQL endpoint, so no CORS needed. Redirect / → /ui/.
    if web_root is not None and web_root.is_dir():
        routes.append(Mount("/ui", app=StaticFiles(directory=str(web_root), html=True)))
        routes.append(Route("/", lambda _req: RedirectResponse("/ui/"), methods=["GET"]))
        logger.info("Web client served at /ui/ from %s", web_root)
    else:
        if web_root is not None:
            logger.warning("web_root %s does not exist — web client not served", web_root)

    routes.append(Mount("/", app=protected_graphql))  # catch-all, must come last
    return Router(routes)


def create_app() -> ASGIApp:
    """The real entrypoint (uvicorn factory mode) — loads config, opens
    the one shared DB connection (SCOPE.md §11.2 addendum), builds the app."""
    cfg = config.load_config()
    db.connect(cfg.db_path)
    config.set_current(cfg)  # A.8 — Sonarr/Radarr credentials for metadata.py
    return build_app(
        cfg.bearer_token,
        cfg.sonarr_webhook_secret,
        cfg.radarr_webhook_secret,
        cfg.web_root,
    )
