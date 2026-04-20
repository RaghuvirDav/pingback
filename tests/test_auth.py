"""Auth flows: signup, login, session, logout, API key rotation."""
from __future__ import annotations


def test_signup_creates_account_and_sets_session(client):
    r = client.post(
        "/signup",
        data={"email": "new@example.com", "name": "New"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard" in r.headers.get("location", "")
    # Session cookie should be set
    assert any(c.name == "pb_session" for c in client.cookies.jar)


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
