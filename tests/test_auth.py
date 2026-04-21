"""Auth flows: signup, login, session, logout, API key rotation."""
from __future__ import annotations


def test_signup_creates_account_and_sets_session(client):
    r = client.post(
        "/signup",
        data={"email": "new@example.com", "name": "New"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # MAK-93: signup now routes through /signup/success so the user can save
    # their API key before landing on the dashboard.
    assert "/signup/success" in r.headers.get("location", "")
    # Session + reveal cookies should both be set.
    set_cookie = r.headers.get("set-cookie", "")
    assert "pb_session=" in set_cookie
    assert "pb_signup_reveal=" in set_cookie


def test_signup_rejects_duplicate_email(client):
    client.post("/signup", data={"email": "dup@example.com"})
    r = client.post("/signup", data={"email": "dup@example.com"})
    assert r.status_code == 409
    assert "already exists" in r.text.lower()


def test_signup_dedup_is_case_insensitive(client):
    """email_hash is computed on the normalised (lowercased, stripped) email,
    so `Alice@Example.com` must collide with `alice@example.com`."""
    client.post("/signup", data={"email": "Alice@Example.com"})
    r = client.post("/signup", data={"email": "  alice@example.com  "})
    assert r.status_code == 409, r.text[:300]


def test_hash_email_helper_normalisation():
    """Unit test for the pure helper — whitespace and case must not matter."""
    from pingback.auth import hash_email

    assert hash_email("Alice@Example.com") == hash_email("alice@example.com")
    assert hash_email("  alice@example.com  ") == hash_email("alice@example.com")
    assert hash_email("a@b.com") != hash_email("c@d.com")


def test_dashboard_accessible_after_signup(client):
    client.post("/signup", data={"email": "dash@example.com"}, follow_redirects=False)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Overview" in r.text


def test_signup_success_reveals_api_key(client):
    """MAK-93: /signup/success must render the plaintext API key exactly once
    per reveal window, with copy/download affordances and a clear warning."""
    r = client.post(
        "/signup",
        data={"email": "reveal@example.com", "name": "Reveal"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/signup/success"

    r2 = client.get("/signup/success")
    assert r2.status_code == 200
    # Key is rendered readonly so the input can't be repurposed as a form field.
    assert "Your API key" in r2.text
    assert 'readonly' in r2.text
    assert "Save it now" in r2.text
    assert 'download="pingback-api-key.txt"' in r2.text
    assert "reveal@example.com" in r2.text
    # Response should not be cached by intermediaries/browsers.
    assert "no-store" in r2.headers.get("cache-control", "").lower()


def test_signup_success_requires_reveal_cookie(client):
    """Without the reveal cookie, /signup/success must not re-render the key;
    it should redirect to the dashboard instead. Guards against back-button
    or bookmark-based re-exposure after the one-time reveal is acknowledged."""
    client.post("/signup", data={"email": "gate@example.com"}, follow_redirects=False)
    # Drop the reveal marker, keep the session cookie — simulates post-Continue.
    client.cookies.delete("pb_signup_reveal")
    r = client.get("/signup/success", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/dashboard" in r.headers.get("location", "")


def test_signup_continue_clears_reveal_cookie_and_redirects(client):
    """Clicking Continue on /signup/success must clear the reveal marker and
    land the user on the dashboard welcome view."""
    client.post("/signup", data={"email": "cont@example.com"}, follow_redirects=False)
    r = client.post("/signup/continue", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard?welcome=1"
    # Subsequent reveal attempts should now redirect away from the key page.
    r2 = client.get("/signup/success", follow_redirects=False)
    assert r2.status_code in (302, 303, 307)
    assert "/dashboard" in r2.headers.get("location", "")


def test_signup_continue_honours_upgrade_pro(client):
    client.post(
        "/signup",
        data={"email": "prokey@example.com", "upgrade": "pro"},
        follow_redirects=False,
    )
    r = client.post("/signup/continue", data={"upgrade": "pro"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pricing?signed_up=1"


def test_logout_clears_session(client):
    client.post("/signup", data={"email": "bye@example.com"}, follow_redirects=False)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    # After logout, dashboard should redirect back to login.
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


def test_login_requires_valid_api_key(client):
    r = client.post(
        "/login",
        data={"api_key": "pb_nope_not_a_real_key"},
        follow_redirects=False,
    )
    # Login failure renders the login page again with an error.
    assert r.status_code == 401
    assert "Invalid API key" in r.text


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
