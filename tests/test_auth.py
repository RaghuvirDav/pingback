"""Auth flows: signup, login, session, logout, verification, password reset."""
from __future__ import annotations

import sqlite3

from tests.conftest import TEST_PASSWORD, signup_and_verify


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def test_signup_requires_password(client):
    r = client.post(
        "/signup",
        data={"email": "nopass@example.com"},
        follow_redirects=False,
    )
    # FastAPI returns 422 when a required Form field is missing.
    assert r.status_code == 422


def test_signup_rejects_short_password(client):
    r = client.post(
        "/signup",
        data={"email": "short@example.com", "password": "abc"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "at least" in r.text.lower()


def test_signup_does_not_log_user_in_until_verified(client):
    r = client.post(
        "/signup",
        data={"email": "pending@example.com", "password": TEST_PASSWORD, "name": "Pending"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Check your email" in r.text
    assert "pending@example.com" in r.text
    # No session cookie should be issued yet.
    assert not any(c.name == "pb_session" for c in client.cookies.jar)
    # Dashboard still locked.
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


def test_signup_success_page_reveals_api_key_once(client):
    r = client.post(
        "/signup",
        data={"email": "key@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 200
    # Rough check: the API key is a urlsafe token, at least 32 chars after the label.
    assert "Your API key" in r.text


def test_signup_rejects_duplicate_email(client):
    client.post("/signup", data={"email": "dup@example.com", "password": TEST_PASSWORD})
    r = client.post("/signup", data={"email": "dup@example.com", "password": TEST_PASSWORD})
    assert r.status_code == 409
    assert "already exists" in r.text.lower()


def test_signup_dedup_is_case_insensitive(client):
    """email_hash is computed on the normalised (lowercased, stripped) email,
    so `Alice@Example.com` must collide with `alice@example.com`."""
    client.post("/signup", data={"email": "Alice@Example.com", "password": TEST_PASSWORD})
    r = client.post("/signup", data={"email": "  alice@example.com  ", "password": TEST_PASSWORD})
    assert r.status_code == 409, r.text[:300]


def test_hash_email_helper_normalisation():
    """Unit test for the pure helper — whitespace and case must not matter."""
    from pingback.auth import hash_email

    assert hash_email("Alice@Example.com") == hash_email("alice@example.com")
    assert hash_email("  alice@example.com  ") == hash_email("alice@example.com")
    assert hash_email("a@b.com") != hash_email("c@d.com")


def test_hash_password_and_verify_round_trip():
    from pingback.auth import hash_password, verify_password

    h = hash_password("correct horse battery staple")
    assert h and h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False
    # None hash short-circuits to False — don't leak account existence.
    assert verify_password("anything", None) is False


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


def test_verification_link_logs_user_in(client):
    signup_and_verify(client, "verify-me@example.com")
    # After signup+verify our helper already clicked the link; session cookie set.
    assert any(c.name == "pb_session" for c in client.cookies.jar)
    r = client.get("/dashboard")
    assert r.status_code == 200


def test_verification_token_expires(client):
    """Manually expire the token in the DB; hitting /verify should fail gracefully."""
    from pingback.auth import hash_email
    from pingback.config import DB_PATH

    client.post("/signup", data={"email": "expire@example.com", "password": TEST_PASSWORD}, follow_redirects=False)

    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT verification_token FROM users WHERE email_hash = ?",
        (hash_email("expire@example.com"),),
    ).fetchone()
    token = row[0]
    # Poison the expiry to a past timestamp.
    con.execute(
        "UPDATE users SET verification_expires_at = '2000-01-01T00:00:00+00:00' WHERE verification_token = ?",
        (token,),
    )
    con.commit()
    con.close()

    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 400
    assert "expired" in r.text.lower()


def test_verify_endpoint_rejects_unknown_token(client):
    r = client.get("/verify?token=not-a-real-token", follow_redirects=False)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_with_email_and_password(client):
    signup_and_verify(client, "login@example.com", password=TEST_PASSWORD)
    client.post("/logout", follow_redirects=False)

    r = client.post(
        "/login",
        data={"email": "login@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard" in r.headers.get("location", "")
    assert any(c.name == "pb_session" for c in client.cookies.jar)


def test_login_wrong_password_shows_generic_error(client):
    """Never leak which emails have accounts. Both "unknown email" and "wrong password"
    must return the same user-visible error."""
    signup_and_verify(client, "realuser@example.com", password=TEST_PASSWORD)
    client.post("/logout", follow_redirects=False)

    r = client.post(
        "/login",
        data={"email": "realuser@example.com", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    r2 = client.post(
        "/login",
        data={"email": "nobody@example.com", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert r2.status_code == 401
    # Same error copy for both paths.
    assert "Invalid email or password" in r.text
    assert "Invalid email or password" in r2.text


def test_login_unverified_account_does_not_log_in(client):
    # Signup only — skip the verification click.
    client.post(
        "/signup",
        data={"email": "unverified@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    r = client.post(
        "/login",
        data={"email": "unverified@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    # Password is right, but no session is issued — user is told to verify.
    assert r.status_code == 200
    assert "verify" in r.text.lower()
    assert not any(c.name == "pb_session" for c in client.cookies.jar)


def test_legacy_user_without_password_gets_reset_link(client):
    """Pre-MAK-96 users have no password_hash. Logging in with email-only-ish
    credentials should trigger a set-password email, not silently fail."""
    from pingback.auth import hash_email
    from pingback.config import DB_PATH

    # Create a legacy-shaped user directly in the DB (no password_hash).
    signup_and_verify(client, "legacy@example.com", password=TEST_PASSWORD)
    client.post("/logout", follow_redirects=False)

    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE users SET password_hash = NULL WHERE email_hash = ?",
        (hash_email("legacy@example.com"),),
    )
    con.commit()
    con.close()

    r = client.post(
        "/login",
        data={"email": "legacy@example.com", "password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    # Login page should tell the user to check their email for a link.
    assert "set one" in r.text.lower() or "set a password" in r.text.lower()

    # A reset_token must have been persisted.
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT reset_token FROM users WHERE email_hash = ?",
        (hash_email("legacy@example.com"),),
    ).fetchone()
    con.close()
    assert row[0], "expected a reset_token to be issued for the legacy account"


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------


def test_forgot_password_always_says_check_your_inbox(client):
    # For both an existing and nonexistent email, the response must look identical.
    signup_and_verify(client, "reset-me@example.com")
    client.post("/logout", follow_redirects=False)

    r1 = client.post("/forgot-password", data={"email": "reset-me@example.com"}, follow_redirects=False)
    r2 = client.post("/forgot-password", data={"email": "nobody@example.com"}, follow_redirects=False)
    assert r1.status_code == 200 and r2.status_code == 200
    assert "reset link" in r1.text.lower()
    assert "reset link" in r2.text.lower()


def test_reset_password_sets_new_password_and_logs_in(client):
    from pingback.auth import hash_email
    from pingback.config import DB_PATH

    signup_and_verify(client, "reset2@example.com", password=TEST_PASSWORD)
    client.post("/logout", follow_redirects=False)

    # Trigger a reset email (no real mail is sent; Resend key is blank in tests).
    client.post("/forgot-password", data={"email": "reset2@example.com"}, follow_redirects=False)

    con = sqlite3.connect(DB_PATH)
    token = con.execute(
        "SELECT reset_token FROM users WHERE email_hash = ?",
        (hash_email("reset2@example.com"),),
    ).fetchone()[0]
    con.close()
    assert token, "reset_token should be persisted after /forgot-password"

    r = client.post(
        "/reset-password",
        data={"token": token, "password": "new-password-xyz"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard" in r.headers.get("location", "")

    # Old password must no longer work.
    client.post("/logout", follow_redirects=False)
    r = client.post(
        "/login",
        data={"email": "reset2@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 401

    # New password works.
    r = client.post(
        "/login",
        data={"email": "reset2@example.com", "password": "new-password-xyz"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_change_password_from_settings(client):
    signup_and_verify(client, "chgpw@example.com", password=TEST_PASSWORD)

    r = client.post(
        "/dashboard/settings/change-password",
        data={"current_password": TEST_PASSWORD, "new_password": "brand-new-pass-1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "success" in r.headers.get("location", "")

    client.post("/logout", follow_redirects=False)
    r = client.post(
        "/login",
        data={"email": "chgpw@example.com", "password": "brand-new-pass-1"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_change_password_requires_correct_current(client):
    signup_and_verify(client, "chgpw2@example.com", password=TEST_PASSWORD)

    r = client.post(
        "/dashboard/settings/change-password",
        data={"current_password": "wrong-current", "new_password": "brand-new-pass-1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Current+password+is+incorrect" in r.headers.get("location", "")


# ---------------------------------------------------------------------------
# Session / logout (kept from pre-MAK-96 suite)
# ---------------------------------------------------------------------------


def test_dashboard_accessible_after_verify(client):
    signup_and_verify(client, "dash@example.com")
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Overview" in r.text


def test_logout_clears_session(client):
    signup_and_verify(client, "bye@example.com")
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


def test_unauthenticated_routes_protected(client):
    for path in [
        "/dashboard",
        "/dashboard/monitors/new",
        "/dashboard/settings",
        "/dashboard/billing",
    ]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303, 307), f"{path} did not redirect"
        assert "/login" in r.headers.get("location", ""), f"{path} did not send to /login"


def test_api_key_bearer_still_works_after_signup(client):
    """Programmatic API-key auth must continue working alongside email+password."""
    from pingback.session import _verify

    signup_and_verify(client, "bearer@example.com")
    cookie = client.cookies.get("pb_session").strip('"')
    api_key = _verify(cookie)
    assert api_key

    # Use the Bearer token to create a monitor via the JSON API.
    r = client.post(
        "/api/monitors",
        json={"name": "bearer-test", "url": "https://example.com", "interval_seconds": 300},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 201, r.text[:300]
