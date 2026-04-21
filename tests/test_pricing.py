"""Public pricing page + upgrade surfaces (MAK-83).

Hits the live FastAPI router via TestClient so route mounting and template
rendering both run for real — no mocks.
"""
from __future__ import annotations

import sqlite3


def test_pricing_page_renders_for_guest(client):
    r = client.get("/pricing")
    assert r.status_code == 200
    assert "/static/app.css" in r.text
    # Both tiers and key facts are shown
    assert "FREE" in r.text and "PRO" in r.text
    assert "$0" in r.text and "$12" in r.text
    assert "Unlimited monitors" in r.text
    assert "60-second" in r.text
    assert "90-day" in r.text
    # Launch promo band per scope
    assert "LAUNCH25" in r.text
    # Guest CTA goes to signup (with upgrade=pro for the Pro card)
    assert 'href="/signup?upgrade=pro"' in r.text
    assert 'href="/signup"' in r.text


def test_pricing_for_authed_free_user_posts_to_checkout(auth_client):
    r = auth_client.get("/pricing")
    assert r.status_code == 200
    # Authed free user sees a POST checkout form, not the guest signup link
    assert 'action="/dashboard/billing/checkout"' in r.text
    assert "Upgrade to Pro" in r.text
    assert 'href="/signup?upgrade=pro"' not in r.text


def test_landing_nav_links_to_pricing(client):
    r = client.get("/")
    assert r.status_code == 200
    # Nav between Features and Sign in
    pricing_idx = r.text.find('href="/pricing"')
    features_idx = r.text.find('href="/#features"')
    signin_idx = r.text.find('href="/login"')
    assert pricing_idx > 0
    assert features_idx > 0 and features_idx < pricing_idx
    assert signin_idx > 0 and pricing_idx < signin_idx


def test_dashboard_sidebar_shows_upgrade_pill_for_free(auth_client):
    r = auth_client.get("/dashboard")
    assert r.status_code == 200
    # Free plan upgrade pill links to /pricing
    assert "Plan: Free" in r.text
    assert 'href="/pricing"' in r.text
    assert 'data-testid="sidebar-upgrade"' in r.text


def test_dashboard_sidebar_hides_upgrade_pill_for_pro(auth_client, tmp_path):
    # Promote the user to pro directly in the DB and re-fetch the dashboard.
    import os

    db_path = os.environ["DB_PATH"]
    con = sqlite3.connect(db_path)
    con.execute("UPDATE users SET plan = 'pro' WHERE email_hash IS NOT NULL")
    con.commit()
    con.close()

    r = auth_client.get("/dashboard")
    assert r.status_code == 200
    assert 'data-testid="sidebar-upgrade"' not in r.text


def test_signup_with_upgrade_pro_lands_on_pricing_after_verify(client):
    """The upgrade=pro hint has to survive email verification (MAK-96 inserted
    a verify step between signup and the first authed page)."""
    from tests.conftest import TEST_PASSWORD, _verification_token_for

    r = client.post(
        "/signup",
        data={
            "email": "upgrade@example.com",
            "name": "Up",
            "password": TEST_PASSWORD,
            "upgrade": "pro",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    token = _verification_token_for("upgrade@example.com")
    r = client.get(f"/verify?token={token}&upgrade=pro", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/pricing")


def test_signup_default_lands_on_dashboard_after_verify(client):
    from tests.conftest import TEST_PASSWORD, _verification_token_for

    r = client.post(
        "/signup",
        data={"email": "default@example.com", "name": "Def", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 200
    token = _verification_token_for("default@example.com")
    r = client.get(f"/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303
    assert "/dashboard" in r.headers["location"]


def test_signup_get_with_upgrade_renders_hidden_field(client):
    r = client.get("/signup?upgrade=pro")
    assert r.status_code == 200
    assert 'name="upgrade"' in r.text
    assert 'value="pro"' in r.text


def test_monitor_limit_error_includes_upgrade_button_for_free(auth_client):
    """When a free user trips the monitor cap, the form shows an Upgrade CTA."""
    # Free cap is 5; create 5 valid monitors first.
    for i in range(5):
        r = auth_client.post(
            "/dashboard/monitors/new",
            data={
                "name": f"m{i}",
                "url": f"https://example.com/{i}",
                "interval_seconds": "300",
            },
            follow_redirects=False,
        )
        assert r.status_code in (200, 302, 303)

    # Sixth attempt should hit the gate.
    r = auth_client.post(
        "/dashboard/monitors/new",
        data={
            "name": "over",
            "url": "https://example.com/over",
            "interval_seconds": "300",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "Upgrade to Pro" in r.text
    assert 'href="/pricing"' in r.text
