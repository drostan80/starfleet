"""Web UI authentication — login gate for the HTML client.

Cookie-based session auth, independent of the GraphQL bearer-token
middleware.  Endpoints are plain Starlette route handlers mounted
before the bearer-token-protected GraphQL catch-all in server.py.

Session flow:
  1. POST /auth/login  → validate credentials → Set-Cookie: sf_session
  2. GET  /auth/check  → nginx auth_request target → 200 or 401
  3. POST /auth/logout → delete session row + clear cookie

Shared settings (LCARS URL, bearer token, home server host, TMDB API
key) are stored in the `web_setting` table so they survive across
browsers.  Machine-specific settings (mpv helper URL) stay in
localStorage on the client.
"""

from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime, timedelta

import bcrypt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from lcars import db, ids

# Session cookie config.
COOKIE_NAME = "sf_session"
SESSION_TTL_DAYS = 30
# Short-lived media tokens — in-memory, for mpv playback auth.
MEDIA_TOKEN_TTL_SECONDS = 300  # 5 minutes
_media_tokens: dict[str, datetime] = {}  # token → expires_at (UTC)
_media_tokens_lock = threading.Lock()
# Allowed shared-setting keys — everything else stays in localStorage.
SHARED_SETTING_KEYS = frozenset({
    "lcars_url",
    "lcars_token",
    "home_server_host",
    "tmdb_api_key",
})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _session_expires() -> str:
    return (datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS)).isoformat()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="strict",
        path="/",
        # No secure=True — LAN HTTP, not HTTPS.
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _get_session_user(request: Request):
    """Return the user row for the session cookie, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    conn = db.get_connection()
    now = _now_iso()
    row = conn.execute(
        "SELECT ws.user_id, ws.expires_at, wu.username"
        " FROM web_session ws"
        " JOIN web_user wu ON wu.id = ws.user_id"
        " WHERE ws.token = ? AND ws.expires_at > ?",
        (token, now),
    ).fetchone()
    if row is None:
        return None
    # Slide the expiry only when less than half the TTL remains —
    # avoids an fsync on every auth_request (fired on every nginx hit).
    halfway = (datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS / 2)).isoformat()
    if row["expires_at"] < halfway:
        conn.execute(
            "UPDATE web_session SET expires_at = ? WHERE token = ?",
            (_session_expires(), token),
        )
        conn.commit()
    return row


def _purge_expired_sessions(conn) -> None:
    """Delete expired sessions — lightweight housekeeping."""
    conn.execute("DELETE FROM web_session WHERE expires_at < ?", (_now_iso(),))


def _purge_media_tokens() -> None:
    """Remove expired media tokens — called under _media_tokens_lock."""
    now = datetime.now(UTC)
    expired = [t for t, exp in _media_tokens.items() if exp <= now]
    for t in expired:
        del _media_tokens[t]


def _validate_media_token(token: str) -> bool:
    """Return True if the token is valid and not expired."""
    with _media_tokens_lock:
        exp = _media_tokens.get(token)
        if exp is None:
            return False
        if datetime.now(UTC) >= exp:
            del _media_tokens[token]
            return False
        return True


# ── Route handlers ───────────────────────────────────────────────


async def login(request: Request) -> Response:
    """POST /auth/login — validate credentials, issue session cookie."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    username = (body.get("username") or "").strip()
    password = (body.get("password") or "")
    if not username or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)

    conn = db.get_connection()
    user = conn.execute(
        "SELECT id, username, password FROM web_user WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()

    if user is None or not bcrypt.checkpw(
        password.encode("utf-8"), user["password"].encode("utf-8")
    ):
        return JSONResponse({"error": "invalid credentials"}, status_code=401)

    # Create session.
    _purge_expired_sessions(conn)
    token = secrets.token_hex(32)
    now = _now_iso()
    conn.execute(
        "INSERT INTO web_session (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user["id"], now, _session_expires()),
    )
    conn.commit()

    resp = JSONResponse({"ok": True, "username": user["username"]})
    _set_session_cookie(resp, token)
    return resp


async def logout(request: Request) -> Response:
    """POST /auth/logout — delete session, clear cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        conn = db.get_connection()
        conn.execute("DELETE FROM web_session WHERE token = ?", (token,))
        conn.commit()
    resp = JSONResponse({"ok": True})
    _clear_session_cookie(resp)
    return resp


async def check(request: Request) -> Response:
    """GET /auth/check — nginx auth_request target.

    Accepts either the session cookie (normal browser requests) or an
    X-Media-Token header (short-lived token for mpv / external players
    that can't carry cookies).
    """
    user = _get_session_user(request)
    if user is not None:
        return Response(status_code=200)
    # Fall back to media token (forwarded by nginx from ?t= query param).
    media_token = request.headers.get("x-media-token", "")
    if media_token and _validate_media_token(media_token):
        return Response(status_code=200)
    return Response(status_code=401)


async def settings_handler(request: Request) -> Response:
    """GET/PUT /auth/settings — shared settings CRUD."""
    user = _get_session_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    conn = db.get_connection()

    if request.method == "GET":
        rows = conn.execute(
            "SELECT key, value FROM web_setting WHERE key IN ({})".format(
                ",".join("?" for _ in SHARED_SETTING_KEYS)
            ),
            tuple(SHARED_SETTING_KEYS),
        ).fetchall()
        result = {r["key"]: r["value"] for r in rows}
        return JSONResponse(result)

    # PUT
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    for key in SHARED_SETTING_KEYS:
        value = body.get(key)
        if value is not None:
            conn.execute(
                "INSERT INTO web_setting (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        else:
            # If explicitly null/absent, remove it.
            conn.execute("DELETE FROM web_setting WHERE key = ?", (key,))
    conn.commit()
    return JSONResponse({"ok": True})


async def me(request: Request) -> Response:
    """GET /auth/me — current user info."""
    user = _get_session_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return JSONResponse({"username": user["username"]})


async def setup(request: Request) -> Response:
    """GET/POST /auth/setup — first-time account creation.

    GET  → {"needs_setup": true/false}
    POST → create first user (only works when 0 users exist)
    """
    conn = db.get_connection()

    if request.method == "GET":
        count = conn.execute("SELECT count(*) FROM web_user").fetchone()[0]
        return JSONResponse({"needs_setup": count == 0})

    # POST — create first user.
    count = conn.execute("SELECT count(*) FROM web_user").fetchone()[0]
    if count > 0:
        return JSONResponse(
            {"error": "setup already completed — use the login form"},
            status_code=409,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    username = (body.get("username") or "").strip()
    password = (body.get("password") or "")
    if not username or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now = _now_iso()
    user_id = ids.generate_id(conn, "u")
    conn.execute(
        "INSERT INTO web_user (id, username, password, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, username, hashed, now, now),
    )

    # Import shared settings if provided.
    imported_settings = body.get("settings")
    if isinstance(imported_settings, dict):
        for key in SHARED_SETTING_KEYS:
            value = imported_settings.get(key)
            if value:
                conn.execute(
                    "INSERT INTO web_setting (key, value) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )

    # Create session.
    token = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO web_session (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now, _session_expires()),
    )
    conn.commit()

    resp = JSONResponse({"ok": True, "username": username})
    _set_session_cookie(resp, token)
    return resp


async def change_password(request: Request) -> Response:
    """POST /auth/change-password — authenticated password change."""
    user = _get_session_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")
    if not current_password or not new_password:
        return JSONResponse(
            {"error": "current_password and new_password required"}, status_code=400
        )
    if len(new_password) < 6:
        return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)

    conn = db.get_connection()
    row = conn.execute(
        "SELECT password FROM web_user WHERE id = ?", (user["user_id"],)
    ).fetchone()
    if not bcrypt.checkpw(
        current_password.encode("utf-8"), row["password"].encode("utf-8")
    ):
        return JSONResponse({"error": "current password is incorrect"}, status_code=401)

    hashed = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn.execute(
        "UPDATE web_user SET password = ?, updated_at = ? WHERE id = ?",
        (hashed, _now_iso(), user["user_id"]),
    )
    conn.commit()
    return JSONResponse({"ok": True})


async def media_token(request: Request) -> Response:
    """POST /auth/media-token — issue a short-lived token for media access.

    Used by the web client's mpv integration: the browser calls this
    (cookie sent automatically), receives a 5-minute token, and appends
    it as ``?t=<token>`` to the ``/files/`` URL passed to the local mpv
    helper.  mpv fetches the URL without cookies, and nginx forwards
    the query-param value as an ``X-Media-Token`` header to
    ``/auth/check``, which validates it.
    """
    user = _get_session_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(seconds=MEDIA_TOKEN_TTL_SECONDS)
    with _media_tokens_lock:
        _purge_media_tokens()
        _media_tokens[token] = expires

    return JSONResponse({"token": token, "expires_in": MEDIA_TOKEN_TTL_SECONDS})
