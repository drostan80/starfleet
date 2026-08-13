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

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import ASGIApp, Receive, Scope, Send

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
    entirely, not inside a resolver or context builder."""

    def __init__(self, app: ASGIApp, bearer_token: str | None) -> None:
        self._app = app
        # A `Bearer <token>` string compared whole, once, via compare_digest
        # — constant-time, and correct even when bearer_token is None (an
        # unconfigured server rejects everything rather than accepting any
        # request, which comparing against "" naively could risk).
        self._expected = f"Bearer {bearer_token}" if bearer_token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive)
        presented = request.headers.get("authorization", "")
        if self._expected is None or not hmac.compare_digest(presented, self._expected):
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
            # could make Sonarr/Radarr treat the connection as broken.
            logger.exception("Webhook to %s: failed to apply", request.url.path)
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": True, **result})

    return view


def build_app(
    bearer_token: str | None,
    sonarr_webhook_secret: str | None = None,
    radarr_webhook_secret: str | None = None,
) -> ASGIApp:
    schema = build_schema()
    graphql_app = GraphQL(schema, context_value=_context_value)
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
        Mount("/", app=protected_graphql),  # last — catch-all, must not shadow the two above
    ]
    return Router(routes)


def create_app() -> ASGIApp:
    """The real entrypoint (uvicorn factory mode) — loads config, opens
    the one shared DB connection (SCOPE.md §11.2 addendum), builds the app."""
    cfg = config.load_config()
    db.connect(cfg.db_path)
    config.set_current(cfg)  # A.8 — Sonarr/Radarr credentials for metadata.py
    return build_app(cfg.bearer_token, cfg.sonarr_webhook_secret, cfg.radarr_webhook_secret)
