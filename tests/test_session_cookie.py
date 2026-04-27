"""Tests for the Secure cookie env-gate (MAK-49 launch-bundle add-on)."""
from __future__ import annotations

import importlib
import sqlite3
import sys

from cryptography.fernet import Fernet


TEST_PASSWORD = "test-password-123"


def _signup_then_verify(client, email: str, db_path: str) -> "httpx.Response":
    """Drive the full MAK-96 signup → verify flow against the given client,
    and return the verify response (the one that sets pb_session)."""
    r = client.post(
        "/signup",
        data={"email": email, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text[:300]
    # Pull the verification token straight out of the test DB — simpler than
    # intercepting Resend in this low-level cookie test.
    from pingback.auth import hash_email

    con = sqlite3.connect(db_path)
    token = con.execute(
        "SELECT verification_token FROM users WHERE email_hash = ?",
        (hash_email(email),),
    ).fetchone()[0]
    con.close()
    assert token
    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r


def _bootstrap_app(monkeypatch, tmp_path, **env):
    """Reload the pingback app with a fresh DB + custom env vars."""
    db_path = tmp_path / "pb.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", env.get("APP_ENV", "development"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("RESEND_API_KEY", "")
    for key in ("SESSION_COOKIE_SECURE",):
        if key in env:
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    pingback_main = importlib.import_module("pingback.main")
    return pingback_main, str(db_path)


def test_cookie_secure_off_by_default_in_dev(monkeypatch, tmp_path):
    """In local dev (no SESSION_COOKIE_SECURE, APP_ENV != production) the
    cookie must be set WITHOUT the Secure flag so HTTP localhost keeps
    working."""
    pingback_main, db_path = _bootstrap_app(monkeypatch, tmp_path)
    from starlette.testclient import TestClient

    with TestClient(pingback_main.app) as c:
        r = _signup_then_verify(c, "nosecure@example.com", db_path)
        set_cookie = r.headers.get("set-cookie", "")
        assert "pb_session=" in set_cookie
        assert "Secure" not in set_cookie, set_cookie
        assert "HttpOnly" in set_cookie
        assert "samesite=lax" in set_cookie.lower()


def test_cookie_secure_enabled_via_env(monkeypatch, tmp_path):
    """When SESSION_COOKIE_SECURE=1 is set (or APP_ENV=production), the
    Set-Cookie header MUST include the Secure flag."""
    pingback_main, db_path = _bootstrap_app(monkeypatch, tmp_path, SESSION_COOKIE_SECURE="1")
    from starlette.testclient import TestClient

    with TestClient(pingback_main.app) as c:
        r = _signup_then_verify(c, "secure@example.com", db_path)
        set_cookie = r.headers.get("set-cookie", "")
        assert "pb_session=" in set_cookie
        assert "Secure" in set_cookie, set_cookie


def test_cookie_secure_enabled_via_app_env_production(monkeypatch, tmp_path):
    """Belt-and-braces: APP_ENV=production should force Secure even when
    SESSION_COOKIE_SECURE is absent."""
    pingback_main, db_path = _bootstrap_app(monkeypatch, tmp_path, APP_ENV="production")
    from starlette.testclient import TestClient

    # APP_ENV=production enables HTTPSRedirectMiddleware — wrap with a
    # `https://` base_url to avoid getting a 307 before the Set-Cookie lands.
    with TestClient(pingback_main.app, base_url="https://testserver") as c:
        r = _signup_then_verify(c, "prod@example.com", db_path)
        set_cookie = r.headers.get("set-cookie", "")
        assert "Secure" in set_cookie, set_cookie
