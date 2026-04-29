"""Tests for the internal admin dashboard at /admin (MAK-142)."""
from __future__ import annotations

import importlib
import sys

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient


ADMIN_EMAIL = "ops@example.com"
NORMAL_EMAIL = "user@example.com"


def _import_app_with_admins(monkeypatch, tmp_path, admin_emails: str):
    """Mirror conftest.app_ctx but inject ADMIN_EMAILS before import.

    `pingback.routes.admin` binds ADMIN_EMAILS at import time, so the env var
    has to be in place before the module is imported.
    """
    db_path = tmp_path / "pingback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("PADDLE_API_KEY", "")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("PADDLE_CLIENT_TOKEN", "")
    monkeypatch.setenv("PADDLE_PRICE_ID_MONTHLY", "")
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("ADMIN_EMAILS", admin_emails)

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]

    return importlib.import_module("pingback.main")


@pytest.fixture
def admin_client(monkeypatch, tmp_path):
    from tests.conftest import install_csrf_autoinject

    app_mod = _import_app_with_admins(monkeypatch, tmp_path, ADMIN_EMAIL)
    with TestClient(app_mod.app) as c:
        install_csrf_autoinject(c)
        yield c


@pytest.fixture
def closed_client(monkeypatch, tmp_path):
    """ADMIN_EMAILS empty — admin route should be fully closed (404)."""
    from tests.conftest import install_csrf_autoinject

    app_mod = _import_app_with_admins(monkeypatch, tmp_path, "")
    with TestClient(app_mod.app) as c:
        install_csrf_autoinject(c)
        yield c


def test_admin_route_404s_when_allowlist_empty(closed_client):
    from tests.conftest import signup_and_verify
    signup_and_verify(closed_client, ADMIN_EMAIL)
    r = closed_client.get("/admin", follow_redirects=False)
    assert r.status_code == 404


def test_admin_route_404s_for_anon(admin_client):
    r = admin_client.get("/admin", follow_redirects=False)
    assert r.status_code == 404


def test_admin_route_404s_for_non_admin(admin_client):
    from tests.conftest import signup_and_verify
    signup_and_verify(admin_client, NORMAL_EMAIL)
    r = admin_client.get("/admin", follow_redirects=False)
    assert r.status_code == 404


def test_admin_route_renders_for_admin(admin_client):
    from tests.conftest import signup_and_verify
    signup_and_verify(admin_client, ADMIN_EMAIL)
    r = admin_client.get("/admin")
    assert r.status_code == 200
    body = r.text
    assert "Admin · Pingback ops" in body
    assert "Total users" in body
    assert "Paid users" in body
    assert "Active monitors" in body
    assert "Recent failures" in body


def test_admin_allowlist_is_case_insensitive(monkeypatch, tmp_path):
    app_mod = _import_app_with_admins(
        monkeypatch, tmp_path, "Ops@Example.COM,other@example.com"
    )
    with TestClient(app_mod.app) as c:
        from tests.conftest import install_csrf_autoinject, signup_and_verify
        install_csrf_autoinject(c)
        # User signs up with lowercase; allowlist is uppercase. Should still match.
        signup_and_verify(c, "ops@example.com")
        r = c.get("/admin")
        assert r.status_code == 200


def test_admin_counts_reflect_users_and_plans(admin_client):
    from tests.conftest import signup_and_verify
    import sqlite3
    from pingback.config import DB_PATH

    # Three signups: admin (free), one free, one we'll bump to pro.
    signup_and_verify(admin_client, ADMIN_EMAIL)
    # Sign out admin so the next signups don't share its session cookie.
    admin_client.post("/logout", follow_redirects=False)
    signup_and_verify(admin_client, "free-user@example.com")
    admin_client.post("/logout", follow_redirects=False)
    signup_and_verify(admin_client, "pro-user@example.com")
    admin_client.post("/logout", follow_redirects=False)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET plan = 'pro' WHERE id IN (SELECT id FROM users WHERE email_hash = ?)",
            (_email_hash("pro-user@example.com"),),
        )
        conn.commit()

    # Log admin back in.
    r = admin_client.post(
        "/login",
        data={"email": ADMIN_EMAIL, "password": "test-password-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = admin_client.get("/admin")
    assert r.status_code == 200
    body = r.text
    # Total users = 3, paid = 1 (pro). Stats render as bare numbers in the cards.
    assert ">3<" in body  # total users card
    assert ">1<" in body  # paid users card (pro)
    # Per-plan summary line on the total-users card.
    assert "free 2" in body
    assert "pro 1" in body
    assert "business 0" in body


def test_admin_lists_active_monitors_with_owner_email(admin_client):
    from tests.conftest import signup_and_verify
    import sqlite3
    import uuid
    from pingback.config import DB_PATH

    signup_and_verify(admin_client, ADMIN_EMAIL)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        user_id = conn.execute(
            "SELECT id FROM users WHERE email_hash = ?",
            (_email_hash(ADMIN_EMAIL),),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO monitors (id, user_id, name, url, interval_seconds, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, "Pingback API",
             "https://api.example.com/health", 300, "active"),
        )
        conn.commit()

    r = admin_client.get("/admin")
    assert r.status_code == 200
    assert "Pingback API" in r.text
    assert "https://api.example.com/health" in r.text
    assert ADMIN_EMAIL in r.text  # owner email is shown


def _email_hash(email: str) -> str:
    from pingback.auth import hash_email
    return hash_email(email)
