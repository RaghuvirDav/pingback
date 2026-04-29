"""CSRF protection (MAK-168).

Covers the double-submit cookie + HMAC token flow added in `pingback/csrf.py`.
The conftest `client` fixture auto-injects a valid token for normal POSTs;
we use `client.raw_post` here to drive the negative paths.
"""
from __future__ import annotations

from tests.conftest import TEST_PASSWORD, signup_and_verify


def _bootstrap_cookie(client) -> str:
    """Hit a GET so the middleware mints `pb_csrf`, then return the value."""
    from pingback.csrf import CSRF_COOKIE_NAME

    client.get("/login")
    cookie = client.cookies.get(CSRF_COOKIE_NAME)
    assert cookie, "middleware did not set pb_csrf"
    return cookie


def test_csrf_cookie_minted_on_first_get(client):
    from pingback.csrf import CSRF_COOKIE_NAME

    assert client.cookies.get(CSRF_COOKIE_NAME) is None
    r = client.get("/login")
    assert r.status_code == 200
    assert client.cookies.get(CSRF_COOKIE_NAME), "Set-Cookie missing on GET"


def test_login_form_renders_csrf_token(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="csrf_token"' in r.text
    # Token is HMAC-SHA256 truncated to 16 bytes → 32 hex chars.
    import re

    match = re.search(r'name="csrf_token"\s+value="([a-f0-9]{32})"', r.text)
    assert match, "expected 32-hex-char csrf_token in rendered form"


def test_post_without_csrf_token_is_403(client):
    _bootstrap_cookie(client)
    r = client.raw_post(  # type: ignore[attr-defined]
        "/login",
        data={"email": "nobody@example.com", "password": "irrelevant"},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text[:200]


def test_post_with_invalid_csrf_token_is_403(client):
    _bootstrap_cookie(client)
    r = client.raw_post(  # type: ignore[attr-defined]
        "/login",
        data={
            "email": "nobody@example.com",
            "password": "irrelevant",
            "csrf_token": "deadbeef" * 4,  # 32 hex chars but wrong HMAC
        },
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_without_csrf_cookie_is_403(client):
    """Cookie missing entirely (e.g. fresh tab attacker) → 403 immediately."""
    from pingback.csrf import CSRF_COOKIE_NAME, compute_csrf_token

    # Compute a token against a value the server doesn't have.
    token = compute_csrf_token("attacker-controlled")
    # Clear any cookie the TestClient might have picked up.
    client.cookies.clear()
    assert client.cookies.get(CSRF_COOKIE_NAME) is None
    r = client.raw_post(  # type: ignore[attr-defined]
        "/login",
        data={"email": "nobody@example.com", "password": "x", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_with_valid_csrf_token_passes(client):
    """Round-trip: GET form, parse token, POST it back, expect non-403."""
    import re

    r = client.get("/login")
    match = re.search(r'name="csrf_token"\s+value="([a-f0-9]{32})"', r.text)
    assert match
    token = match.group(1)

    r = client.raw_post(  # type: ignore[attr-defined]
        "/login",
        data={
            "email": "nobody@example.com",
            "password": "irrelevant",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    # We don't expect a 403 — login itself returns 401 for unknown email,
    # which is the next layer down (rate limiter passes, CSRF passes).
    assert r.status_code != 403, r.text[:200]


def test_authenticated_form_post_protected(auth_client):
    """A logged-in user without a CSRF token still gets 403 on settings save."""
    r = auth_client.raw_post(  # type: ignore[attr-defined]
        "/dashboard/settings/notifications",
        data={"digest_enabled": "1", "timezone_name": "Etc/UTC"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_authenticated_form_post_with_token_succeeds(auth_client):
    """Same call as above but with the auto-injected token (via wrapper) works."""
    r = auth_client.post(
        "/dashboard/settings/notifications",
        data={
            "digest_enabled": "1",
            "timezone_name": "Etc/UTC",
            "redirect_to": "/dashboard/settings",
        },
        follow_redirects=False,
    )
    # 303 redirect on success, never 403.
    assert r.status_code == 303, r.text[:200]


def test_logout_requires_csrf(auth_client):
    r = auth_client.raw_post("/logout", follow_redirects=False)  # type: ignore[attr-defined]
    assert r.status_code == 403


def test_paddle_webhook_is_not_csrf_protected(client):
    """Webhook auth is the Paddle signature, not CSRF — no token required."""
    # A bogus body returns 503 (webhook secret unset in tests) or 400 (bad sig).
    # Either way, NOT 403, because csrf_protect is not in the dependency chain.
    r = client.raw_post(  # type: ignore[attr-defined]
        "/api/paddle/webhook",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert r.status_code != 403


def test_signup_via_helper_still_works(client):
    """`signup_and_verify` posts a form — the auto-injecting client wrapper
    must keep it working after CSRF is enforced."""
    signup_and_verify(client, "csrf-roundtrip@example.com", password=TEST_PASSWORD)
    # If we got here without an AssertionError, the helper round-tripped.
    r = client.get("/dashboard")
    assert r.status_code == 200
