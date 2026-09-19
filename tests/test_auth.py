"""Tests for the web UI auth endpoints (login, check, logout, setup,
settings, change-password).
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lcars import anilist_client, config, db, fribb
from lcars.server import build_app

BEARER_TOKEN = "test-token-123"


@pytest.fixture
def migrated_db(tmp_path) -> Path:
    db_path = tmp_path / "lcars_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True,
        capture_output=True,
    )
    return db_path


TMDB_API_KEY = "tmdb-test-key"


@pytest.fixture
async def client(migrated_db, monkeypatch):
    db.connect(migrated_db)
    config.set_current(config.Config(bearer_token=BEARER_TOKEN, tmdb_api_key=TMDB_API_KEY))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: None)
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    monkeypatch.setattr(fribb, "load_dataset", lambda: [])
    app = build_app(BEARER_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    db.close()


# ── Setup ─────────────────────────────────────────────────


async def test_setup_needs_setup_true(client):
    """GET /auth/setup returns needs_setup=true when no users exist."""
    r = await client.get("/auth/setup")
    assert r.status_code == 200
    assert r.json()["needs_setup"] is True


async def test_setup_create_account(client):
    """POST /auth/setup creates the first user and sets a session cookie."""
    r = await client.post(
        "/auth/setup",
        json={"username": "drostan", "password": "hunter42"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["username"] == "drostan"
    # Session cookie set.
    assert "sf_session" in r.cookies


async def test_setup_rejects_second_user(client):
    """POST /auth/setup rejects when a user already exists."""
    await client.post("/auth/setup", json={"username": "a", "password": "hunter42"})
    r = await client.post("/auth/setup", json={"username": "b", "password": "hunter42"})
    assert r.status_code == 409


async def test_setup_needs_setup_false_after_create(client):
    """GET /auth/setup returns needs_setup=false after account creation."""
    await client.post("/auth/setup", json={"username": "a", "password": "hunter42"})
    r = await client.get("/auth/setup")
    assert r.json()["needs_setup"] is False


async def test_setup_ignores_settings_body(client):
    """POST /auth/setup no longer imports a client-supplied settings dict
    (A0 — lcars_token/tmdb_api_key are served from LCARS config, never
    stored from a client)."""
    r = await client.post(
        "/auth/setup",
        json={
            "username": "drostan",
            "password": "hunter42",
            "settings": {"lcars_token": "should-be-ignored"},
        },
    )
    assert r.status_code == 200
    cookies = r.cookies
    r2 = await client.get("/auth/settings", cookies=cookies)
    assert r2.status_code == 200
    # Config value wins regardless of what setup's body tried to send.
    assert r2.json()["lcars_token"] == BEARER_TOKEN


async def test_setup_rejects_short_password(client):
    """POST /auth/setup rejects passwords shorter than 6 chars."""
    r = await client.post("/auth/setup", json={"username": "a", "password": "12345"})
    assert r.status_code == 400
    assert "6 characters" in r.json()["error"]


# ── Login / Logout ────────────────────────────────────────


async def _create_user(client) -> dict:
    """Helper: create a user via setup and return the response cookies."""
    r = await client.post(
        "/auth/setup", json={"username": "drostan", "password": "hunter42"}
    )
    return dict(r.cookies)


async def test_login_success(client):
    """POST /auth/login with correct credentials sets session cookie."""
    await _create_user(client)
    r = await client.post(
        "/auth/login", json={"username": "drostan", "password": "hunter42"}
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "sf_session" in r.cookies


async def test_login_wrong_password(client):
    """POST /auth/login rejects wrong password."""
    await _create_user(client)
    r = await client.post(
        "/auth/login", json={"username": "drostan", "password": "wrong"}
    )
    assert r.status_code == 401


async def test_login_case_insensitive_username(client):
    """POST /auth/login matches username case-insensitively."""
    await _create_user(client)
    r = await client.post(
        "/auth/login", json={"username": "DROSTAN", "password": "hunter42"}
    )
    assert r.status_code == 200


async def test_check_valid_session(client):
    """GET /auth/check returns 200 for a valid session."""
    cookies = await _create_user(client)
    r = await client.get("/auth/check", cookies=cookies)
    assert r.status_code == 200


async def test_check_no_session(client):
    """GET /auth/check returns 401 without a session cookie."""
    r = await client.get("/auth/check")
    assert r.status_code == 401


async def test_check_valid_bearer(client):
    """GET /auth/check returns 200 for a valid bearer token, no cookie needed
    (A0 — native code has no cookie jar)."""
    r = await client.get(
        "/auth/check", headers={"Authorization": f"Bearer {BEARER_TOKEN}"}
    )
    assert r.status_code == 200


async def test_check_wrong_bearer(client):
    """GET /auth/check returns 401 for a wrong bearer token."""
    r = await client.get(
        "/auth/check", headers={"Authorization": "Bearer wrong-token"}
    )
    assert r.status_code == 401


async def test_logout(client):
    """POST /auth/logout invalidates the session."""
    cookies = await _create_user(client)
    r = await client.post("/auth/logout", cookies=cookies)
    assert r.status_code == 200
    # The session should be invalidated.
    r2 = await client.get("/auth/check", cookies=cookies)
    assert r2.status_code == 401


# ── Settings ──────────────────────────────────────────────


async def test_settings_get_serves_from_config(client):
    """GET /auth/settings serves lcars_token/tmdb_api_key from LCARS config
    (A0), not from anything a client ever wrote."""
    cookies = await _create_user(client)
    r = await client.get("/auth/settings", cookies=cookies)
    assert r.status_code == 200
    data = r.json()
    assert data["lcars_token"] == BEARER_TOKEN
    assert data["tmdb_api_key"] == TMDB_API_KEY


async def test_settings_put_no_longer_persists_token(client):
    """PUT /auth/settings no longer accepts lcars_token/tmdb_api_key —
    GET keeps serving the config value regardless of what was PUT."""
    cookies = await _create_user(client)
    r = await client.put(
        "/auth/settings",
        json={"lcars_token": "abc", "tmdb_api_key": "xyz"},
        cookies=cookies,
    )
    assert r.status_code == 200
    r2 = await client.get("/auth/settings", cookies=cookies)
    assert r2.json()["lcars_token"] == BEARER_TOKEN
    assert r2.json()["tmdb_api_key"] == TMDB_API_KEY


async def test_settings_rejects_unauthenticated(client):
    """GET/PUT /auth/settings returns 401 without a session."""
    r = await client.get("/auth/settings")
    assert r.status_code == 401


# ── Me ────────────────────────────────────────────────────


async def test_me(client):
    """GET /auth/me returns the current user's username."""
    cookies = await _create_user(client)
    r = await client.get("/auth/me", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["username"] == "drostan"


# ── Change Password ──────────────────────────────────────


async def test_change_password(client):
    """POST /auth/change-password changes the password."""
    cookies = await _create_user(client)
    r = await client.post(
        "/auth/change-password",
        json={"current_password": "hunter42", "new_password": "newpass99"},
        cookies=cookies,
    )
    assert r.status_code == 200
    # Old password no longer works.
    r2 = await client.post(
        "/auth/login", json={"username": "drostan", "password": "hunter42"}
    )
    assert r2.status_code == 401
    # New password works.
    r3 = await client.post(
        "/auth/login", json={"username": "drostan", "password": "newpass99"}
    )
    assert r3.status_code == 200


async def test_change_password_wrong_current(client):
    """POST /auth/change-password rejects wrong current password."""
    cookies = await _create_user(client)
    r = await client.post(
        "/auth/change-password",
        json={"current_password": "wrong", "new_password": "newpass99"},
        cookies=cookies,
    )
    assert r.status_code == 401
