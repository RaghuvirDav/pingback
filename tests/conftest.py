"""Shared pytest fixtures.

Every test gets a fresh SQLite DB file on disk (integration — hits the real
database, no mocks), and a TestClient wired to the FastAPI app.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys

import pytest
from cryptography.fernet import Fernet


TEST_PASSWORD = "test-password-123"


@pytest.fixture
def app_ctx(monkeypatch, tmp_path):
    """Set up env + reimport the app with a fresh DB per test."""
    db_path = tmp_path / "pingback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("PADDLE_API_KEY", "")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("PADDLE_CLIENT_TOKEN", "")
    monkeypatch.setenv("PADDLE_PRICE_ID_MONTHLY", "")
    monkeypatch.setenv("PADDLE_PRICE_ID_YEARLY", "")
    monkeypatch.setenv("PADDLE_DISCOUNT_ID_LAUNCH", "")
    monkeypatch.setenv("RESEND_API_KEY", "")

    # Force re-import so module-level config picks up fresh env vars.
    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]

    pingback_main = importlib.import_module("pingback.main")
    return pingback_main


def install_csrf_autoinject(c):
    """Wrap a TestClient so form POSTs auto-include a valid CSRF token.

    Every state-changing form POST now requires a `csrf_token` field plus a
    matching `pb_csrf` cookie (MAK-168). Tests don't care about the token —
    they care about the route's behaviour — so we transparently inject it.

    Skipped for `json=` and `content=` requests (Paddle webhook, JSON XHR);
    those endpoints aren't CSRF-protected anyway.

    Exposes `client.raw_post` for CSRF tests that need the un-wrapped path.
    """
    from pingback.csrf import CSRF_COOKIE_NAME, compute_csrf_token

    original_post = c.post

    def patched_post(url, *args, **kwargs):
        if kwargs.get("json") is not None or kwargs.get("content") is not None:
            return original_post(url, *args, **kwargs)
        data = kwargs.get("data") or {}
        if isinstance(data, dict) and "csrf_token" not in data:
            cookie_val = c.cookies.get(CSRF_COOKIE_NAME)
            if not cookie_val:
                # Any GET against the app is enough to mint the cookie.
                c.get("/login")
                cookie_val = c.cookies.get(CSRF_COOKIE_NAME)
            if cookie_val:
                kwargs["data"] = {**data, "csrf_token": compute_csrf_token(cookie_val)}
        return original_post(url, *args, **kwargs)

    c.raw_post = original_post  # type: ignore[attr-defined]
    c.post = patched_post  # type: ignore[assignment]
    return c


@pytest.fixture
def client(app_ctx):
    from starlette.testclient import TestClient

    with TestClient(app_ctx.app) as c:
        install_csrf_autoinject(c)
        yield c


def _verification_token_for(email: str) -> str:
    """Read the verification token for `email` straight from the test DB."""
    from pingback.auth import hash_email
    from pingback.config import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT verification_token FROM users WHERE email_hash = ?",
            (hash_email(email),),
        ).fetchone()
    assert row is not None and row["verification_token"], "signup did not persist a verification token"
    return row["verification_token"]


def api_key_for_email(email: str) -> str:
    """Read the user's plaintext API key directly from the test DB.

    MAK-167 stopped putting the API key in the session cookie, so tests that
    used to fish it out of `pb_session` need a different path. The DB still
    holds the encrypted key — decrypt it here for tests that exercise
    Bearer-auth JSON endpoints.
    """
    from pingback.auth import hash_email
    from pingback.config import DB_PATH
    from pingback.encryption import decrypt_value

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT api_key FROM users WHERE email_hash = ?",
            (hash_email(email),),
        ).fetchone()
    assert row is not None and row["api_key"], f"no api_key on file for {email!r}"
    return decrypt_value(row["api_key"])


def signup_and_verify(
    client,
    email: str,
    name: str = "Test User",
    password: str = TEST_PASSWORD,
    upgrade: str = "",
) -> None:
    """Sign a user up, pull their verification token from the DB, click the
    verify URL, and leave the session cookie set on the client.

    Tests don't want to mock outbound email, so this helper does exactly what
    an end user would do (click the link in the verification email) without
    needing Resend to be wired up in the test environment.
    """
    data = {"email": email, "name": name, "password": password}
    if upgrade:
        data["upgrade"] = upgrade
    r = client.post("/signup", data=data, follow_redirects=False)
    assert r.status_code == 200, r.text[:400]

    token = _verification_token_for(email)
    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303, r.text[:400]


@pytest.fixture
def auth_client(client):
    """Return a TestClient with an authenticated user session.

    Creates a fresh user on each invocation via the full signup+verify flow,
    giving the test access to the resulting session cookie (as managed by
    TestClient) and the user's email via client.email.
    """
    email = f"user-{os.urandom(4).hex()}@example.com"
    signup_and_verify(client, email)
    client.email = email  # type: ignore[attr-defined]
    return client
