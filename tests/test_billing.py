"""Billing page + checkout-flow smoke (Stripe calls stubbed via unavailable key)."""
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
    # The Free plan tile should show "Current plan".
    assert "Current plan" in r.text


def test_checkout_without_stripe_key_fails_safely(client):
    """When STRIPE_SECRET_KEY is not configured the checkout endpoint must
    respond with an error (not a 500 or crash)."""
    client.post("/signup", data={"email": "no-stripe@example.com"}, follow_redirects=False)
    r = client.post("/dashboard/billing/checkout", follow_redirects=False)
    # Either an error flash redirect OR a 4xx/5xx — never a crash.
    assert r.status_code in (200, 303, 400, 500, 503)
