"""Tests for the Secure cookie env-gate (MAK-49 launch-bundle add-on)."""
from __future__ import annotations


def test_cookie_secure_off_by_default_in_dev(monkeypatch, tmp_path):
    """In local dev (no SESSION_COOKIE_SECURE, APP_ENV != production) the
    cookie must be set WITHOUT the Secure flag so HTTP localhost keeps
    working."""
    import importlib
    import sys

    from cryptography.fernet import Fernet

    # Clean env — make sure nothing is leaking in
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pb.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    pingback_main = importlib.import_module("pingback.main")

    from starlette.testclient import TestClient

    with TestClient(pingback_main.app) as c:
        r = c.post("/signup", data={"email": "nosecure@example.com"}, follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers.get("set-cookie", "")
        assert "pb_session=" in set_cookie
        assert "Secure" not in set_cookie, set_cookie
        assert "HttpOnly" in set_cookie
        assert "samesite=lax" in set_cookie.lower()


def test_cookie_secure_enabled_via_env(monkeypatch, tmp_path):
    """When SESSION_COOKIE_SECURE=1 is set (or APP_ENV=production), the
    Set-Cookie header MUST include the Secure flag."""
    import importlib
    import sys

    from cryptography.fernet import Fernet

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pb_secure.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    pingback_main = importlib.import_module("pingback.main")

    from starlette.testclient import TestClient

    with TestClient(pingback_main.app) as c:
        r = c.post("/signup", data={"email": "secure@example.com"}, follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers.get("set-cookie", "")
        assert "pb_session=" in set_cookie
        assert "Secure" in set_cookie, set_cookie


def test_cookie_secure_enabled_via_app_env_production(monkeypatch, tmp_path):
    """Belt-and-braces: APP_ENV=production should force Secure even when
    SESSION_COOKIE_SECURE is absent."""
    import importlib
    import sys

    from cryptography.fernet import Fernet

    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pb_prod.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    pingback_main = importlib.import_module("pingback.main")

    from starlette.testclient import TestClient

    # APP_ENV=production enables HTTPSRedirectMiddleware — wrap with a
    # `https://` base_url to avoid getting a 307 before the Set-Cookie lands.
    with TestClient(pingback_main.app, base_url="https://testserver") as c:
        r = c.post("/signup", data={"email": "prod@example.com"}, follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])
        set_cookie = r.headers.get("set-cookie", "")
        assert "Secure" in set_cookie, set_cookie
