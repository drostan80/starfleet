"""The ASGI app — Ariadne schema binding, bearer-token auth, and the
X-LCARS-Client context extraction (SCOPE.md §8, §5.7 addendum).

`create_app()` (not a bare module-level `app`) is the real entrypoint —
deliberately, so building an app is an explicit action with its own DB
connection, not an import-time side effect that would fire on `import
lcars.server` alone (e.g. during test collection). Run via uvicorn's
factory mode: `uvicorn lcars.server:create_app --factory`.
"""

import hmac
from importlib import resources

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lcars import config, db
from lcars.resolvers import BINDABLES


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


def build_app(bearer_token: str | None) -> ASGIApp:
    schema = build_schema()
    graphql_app = GraphQL(schema, context_value=_context_value)
    return BearerTokenMiddleware(graphql_app, bearer_token)


def create_app() -> ASGIApp:
    """The real entrypoint (uvicorn factory mode) — loads config, opens
    the one shared DB connection (SCOPE.md §11.2 addendum), builds the app."""
    cfg = config.load_config()
    db.connect(cfg.db_path)
    config.set_current(cfg)  # A.8 — Sonarr/Radarr credentials for metadata.py
    return build_app(cfg.bearer_token)
