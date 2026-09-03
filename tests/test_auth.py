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


@pytest.fixture
async def client(migrated_db, monkeypatch):
    db.connect(migrated_db)
    config.set_current(config.Config())
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


async def test_setup_imports_settings(client):
    """POST /auth/setup with settings imports them into web_setting."""
    r = await client.post(
        "/auth/setup",
        json={
            "username": "drostan",
            "password": "hunter42",
            "settings": {
                "lcars_url": "http://192.168.0.152:8888",
                "lcars_token": "tok-123",
                "home_server_host": "192.168.0.152",
                "tmdb_api_key": "tmdb-key",
                "mpv_helper_url": "http://localhost:19450",  # should be ignored
            },
        },
    )
    assert r.status_code == 200
    # Read back settings using the session cookie.
    cookies = r.cookies
    r2 = await client.get("/auth/settings", cookies=cookies)
    assert r2.status_code == 200
    settings = r2.json()
    assert settings["lcars_url"] == "http://192.168.0.152:8888"
    assert settings["lcars_token"] == "tok-123"
    assert settings["home_server_host"] == "192.168.0.152"
    assert settings["tmdb_api_key"] == "tmdb-key"
    # mpv_helper_url should NOT be stored server-side.
    assert "mpv_helper_url" not in settings


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


async def test_logout(client):
    """POST /auth/logout invalidates the session."""
    cookies = await _create_user(client)
    r = await client.post("/auth/logout", cookies=cookies)
    assert r.status_code == 200
    # The session should be invalidated.
    r2 = await client.get("/auth/check", cookies=cookies)
    assert r2.status_code == 401


# ── Settings ──────────────────────────────────────────────


async def test_settings_put_and_get(client):
    """PUT /auth/settings stores values, GET reads them back."""
    cookies = await _create_user(client)
    r = await client.put(
        "/auth/settings",
        json={"lcars_url": "http://test:8888", "lcars_token": "abc"},
        cookies=cookies,
    )
    assert r.status_code == 200
    r2 = await client.get("/auth/settings", cookies=cookies)
    assert r2.json()["lcars_url"] == "http://test:8888"
    assert r2.json()["lcars_token"] == "abc"


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
