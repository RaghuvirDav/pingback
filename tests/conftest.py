"""Shared pytest fixtures.

Every test gets a fresh SQLite DB file on disk (integration — hits the real
database, no mocks), and a TestClient wired to the FastAPI app.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def app_ctx(monkeypatch, tmp_path):
    """Set up env + reimport the app with a fresh DB per test."""
    db_path = tmp_path / "pingback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
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


@pytest.fixture
def auth_client(client):
    """Return a TestClient with an authenticated user session.

    Creates a fresh user on each invocation via the signup flow, giving the
    test access to the resulting session cookie (as managed by TestClient) and
    the user's API key via the auth module.
    """
    email = f"user-{os.urandom(4).hex()}@example.com"
    r = client.post("/signup", data={"email": email, "name": "Test User"}, follow_redirects=False)
    assert r.status_code == 303
    client.email = email  # type: ignore[attr-defined]
    return client
