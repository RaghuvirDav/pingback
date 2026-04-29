"""MAK-167 regression tests.

Pin the post-rewrite invariants of the cookie-based session:

- the cookie value never embeds the user's plaintext API key
- the cookie value never embeds the user's stored API key hash
- a successful login rotates the session id (any pre-login cookie is dead)
- a successful password change rotates the session id
- ``validate_secrets()`` refuses to boot in production with default secrets
"""
from __future__ import annotations

import importlib
import sqlite3
import sys

import pytest
from cryptography.fernet import Fernet

from tests.conftest import (
    TEST_PASSWORD,
    api_key_for_email,
    install_csrf_autoinject,
    signup_and_verify,
)


def _raw_cookie(client) -> str:
    cookie = client.cookies.get("pb_session")
    assert cookie, "pb_session cookie not set"
    return cookie.strip('"')


def test_cookie_value_does_not_contain_api_key(client):
    """Core fix: the cookie used to be `b64(api_key).hmac` — verify the API
    key (and its SHA-256 hash) no longer appear anywhere inside the cookie."""
    from pingback.auth import hash_api_key

    signup_and_verify(client, "decouple@example.com")
    cookie_value = _raw_cookie(client)
    api_key = api_key_for_email("decouple@example.com")

    assert api_key not in cookie_value, (
        "session cookie still leaks the API key (MAK-167 regression)"
    )
    assert hash_api_key(api_key) not in cookie_value, (
        "session cookie embeds the API-key hash"
    )


def test_session_cookie_resolves_via_sessions_table(client):
    """After signup+verify there must be exactly one server-side session row,
    and the cookie's session id must be the row's primary key."""
    from pingback.config import DB_PATH
    from pingback.session import _verify_signed_session_id

    signup_and_verify(client, "row@example.com")
    sid = _verify_signed_session_id(_raw_cookie(client))
    assert sid, "cookie HMAC failed to verify"

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, user_id, expires_at FROM sessions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["expires_at"]  # populated


def test_login_rotates_session_id(client):
    """Logging in must mint a new session id (pre-login cookie should die)."""
    from pingback.config import DB_PATH
    from pingback.session import _verify_signed_session_id

    signup_and_verify(client, "rotate@example.com")
    pre_login_sid = _verify_signed_session_id(_raw_cookie(client))
    assert pre_login_sid

    # Force a fresh login through the password path.
    r = client.post(
        "/login",
        data={"email": "rotate@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:300]

    post_login_sid = _verify_signed_session_id(_raw_cookie(client))
    assert post_login_sid
    assert post_login_sid != pre_login_sid, "session id was not rotated on login"

    # The pre-login session row must be gone.
    with sqlite3.connect(DB_PATH) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ?", (pre_login_sid,)
        ).fetchone()[0]
    assert n == 0


def test_password_change_rotates_session_id(client):
    """Changing the password must rotate the session id and kill all others."""
    from pingback.config import DB_PATH
    from pingback.session import _verify_signed_session_id

    signup_and_verify(client, "chg@example.com")
    pre_sid = _verify_signed_session_id(_raw_cookie(client))

    r = client.post(
        "/dashboard/settings/change-password",
        data={"current_password": TEST_PASSWORD, "new_password": "BrandNewPass-987"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]

    post_sid = _verify_signed_session_id(_raw_cookie(client))
    assert post_sid and post_sid != pre_sid

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
    ids = {r[0] for r in rows}
    assert pre_sid not in ids
    assert post_sid in ids


def test_validate_secrets_rejects_production_without_secrets(monkeypatch, tmp_path):
    """`validate_secrets()` must raise when APP_ENV=production but
    ENCRYPTION_KEY / SESSION_SECRET are missing or use the dev placeholder."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "vs.db"))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENCRYPTION_KEY", "")
    monkeypatch.setenv("SESSION_SECRET", "")

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    config = importlib.import_module("pingback.config")

    with pytest.raises(RuntimeError) as exc:
        config.validate_secrets()
    msg = str(exc.value)
    assert "ENCRYPTION_KEY" in msg
    assert "SESSION_SECRET" in msg


def test_validate_secrets_passes_in_development(monkeypatch, tmp_path):
    """Outside production, `validate_secrets()` must be a no-op even when the
    secrets are blank — that's the local dev experience."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "vs2.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", "")
    monkeypatch.setenv("SESSION_SECRET", "")

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    config = importlib.import_module("pingback.config")

    # Should not raise.
    config.validate_secrets()


def test_validate_secrets_passes_in_production_with_real_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "vs3.db"))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "a-real-session-secret-not-the-dev-default-32-bytes")

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    config = importlib.import_module("pingback.config")
    config.validate_secrets()  # must not raise
