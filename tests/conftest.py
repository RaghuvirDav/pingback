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


@pytest.fixture
def client(app_ctx):
    from starlette.testclient import TestClient

    with TestClient(app_ctx.app) as c:
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
