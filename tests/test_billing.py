"""Billing page smoke: renders all three tiers, marks current plan, and the
Paddle upgrade button is only enabled when client-side creds are configured."""
from __future__ import annotations


def test_billing_page_shows_plans(client):
    client.post("/signup", data={"email": "billing@example.com"}, follow_redirects=False)
    r = client.get("/dashboard/billing")
    assert r.status_code == 200
    assert "FREE" in r.text
    assert "PRO" in r.text
    assert "BUSINESS" in r.text


def test_billing_page_marks_current_plan_for_free(client):
    client.post("/signup", data={"email": "current@example.com"}, follow_redirects=False)
    r = client.get("/dashboard/billing")
    assert r.status_code == 200
    assert "Current plan" in r.text


def test_upgrade_button_disabled_when_paddle_not_configured(client):
    """With no PADDLE_CLIENT_SIDE_TOKEN the upgrade button must render as
    disabled and the Paddle.js bundle must NOT be injected — we don't want
    to load a third-party script on a page that can't use it."""
    client.post("/signup", data={"email": "no-paddle@example.com"}, follow_redirects=False)
    r = client.get("/dashboard/billing")
    assert r.status_code == 200
    assert 'id="upgrade-pro"' in r.text
    assert "disabled" in r.text
    assert "cdn.paddle.com" not in r.text


def test_portal_without_cached_url_redirects_with_error(client):
    """GET /dashboard/billing/portal needs a cached paddle_portal_url. Without
    one, it must redirect back to the billing page — not 500."""
    client.post("/signup", data={"email": "no-portal@example.com"}, follow_redirects=False)
    r = client.get("/dashboard/billing/portal", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/dashboard/billing" in r.headers.get("location", "")
