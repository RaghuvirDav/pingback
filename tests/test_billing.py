"""Billing page + portal flow smoke tests.

Real Paddle API calls are never made — Paddle.js runs client-side in the
browser, and the portal endpoint short-circuits when PADDLE_API_KEY is
blank (the route returns an error redirect rather than calling out)."""
from __future__ import annotations

from tests.conftest import signup_and_verify


def test_billing_page_shows_plans(client):
    signup_and_verify(client, "billing@example.com")
    r = client.get("/dashboard/billing")
    assert r.status_code == 200
    assert "FREE" in r.text
    assert "PRO" in r.text
    assert "BUSINESS" in r.text


def test_billing_page_marks_current_plan_for_free(client):
    signup_and_verify(client, "current@example.com")
    r = client.get("/dashboard/billing")
    assert r.status_code == 200
    assert "Current plan" in r.text


def test_upgrade_button_disabled_when_paddle_not_configured(client):
    """With no PADDLE_CLIENT_TOKEN, the Paddle.js script must not load and the
    upgrade button must be disabled — page still renders, no JS errors."""
    signup_and_verify(client, "noconf@example.com")
    r = client.get("/dashboard/billing")
    assert r.status_code == 200
    assert "paddle-upgrade-btn" in r.text
    assert "disabled" in r.text
    assert "cdn.paddle.com" not in r.text


def test_portal_without_customer_redirects_with_error(client):
    """A free user with no paddle_customer_id can't open the portal — the route
    must redirect back with an error, not 500."""
    signup_and_verify(client, "noportal@example.com")
    r = client.post("/dashboard/billing/portal", follow_redirects=False)
    assert r.status_code in (302, 303)
    location = r.headers.get("location", "")
    assert "/dashboard/billing" in location
    assert "error" in location.lower()
