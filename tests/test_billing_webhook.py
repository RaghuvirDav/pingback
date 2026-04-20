"""Stripe webhook: signature verification, plan transitions, idempotency.

These tests sign payloads with the same secret the webhook route reads and
drive the route via the TestClient, so signature verification runs for real.
The Stripe API itself is never called — we only build the JSON payloads that
Stripe would have sent.
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import time
import uuid
from hashlib import sha256

import pytest
from cryptography.fernet import Fernet


WEBHOOK_SECRET = "whsec_test_secret"
WEBHOOK_URL = "/api/stripe/webhook"


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@pytest.fixture
def billing_app(monkeypatch, tmp_path):
    """Like the shared `app_ctx` but with Stripe credentials populated."""
    import importlib
    import sys

    db_path = tmp_path / "pingback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("STRIPE_PRICE_ID_PRO_MONTHLY", "price_test_pro")
    monkeypatch.setenv("RESEND_API_KEY", "")

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]

    pingback_main = importlib.import_module("pingback.main")
    return pingback_main, str(db_path)


@pytest.fixture
def billing_client(billing_app):
    from starlette.testclient import TestClient

    pingback_main, db_path = billing_app
    with TestClient(pingback_main.app) as c:
        c.db_path = db_path  # type: ignore[attr-defined]
        yield c


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    ts = str(int(time.time()))
    signed = f"{ts}.{payload.decode()}".encode()
    v1 = hmac.new(secret.encode(), signed, sha256).hexdigest()
    return f"t={ts},v1={v1}"


def _signup_with_customer(client, email: str, customer_id: str) -> str:
    """Sign up a user and attach a Stripe customer id. Returns the user id."""
    r = client.post("/signup", data={"email": email}, follow_redirects=False)
    assert r.status_code == 303
    con = sqlite3.connect(client.db_path)
    row = con.execute(
        "SELECT id FROM users WHERE email_hash = ?",
        ((_hash_email(email)),),
    ).fetchone()
    assert row, "signup did not create a user"
    user_id = row[0]
    con.execute(
        "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
        (customer_id, user_id),
    )
    con.commit()
    con.close()
    return user_id


def _hash_email(email: str) -> str:
    from pingback.auth import hash_email

    return hash_email(email)


def _user_plan(client, user_id: str) -> tuple[str, str | None, str | None]:
    con = sqlite3.connect(client.db_path)
    row = con.execute(
        "SELECT plan, stripe_subscription_id, plan_renews_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    con.close()
    return row


def _event(event_type: str, obj: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "api_version": "2024-06-20",
        "type": event_type,
        "data": {"object": obj},
        "created": int(time.time()),
        "livemode": False,
        "pending_webhooks": 0,
        "request": {"id": None, "idempotency_key": None},
    }


def _post_event(client, event: dict, *, secret: str = WEBHOOK_SECRET, tamper: bool = False):
    body = json.dumps(event).encode()
    sig = _sign(body, secret=secret)
    if tamper:
        body = body + b" "  # payload drifts from the signature
    return client.post(
        WEBHOOK_URL,
        content=body,
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def test_webhook_rejects_missing_signature(billing_client):
    r = billing_client.post(WEBHOOK_URL, content=b"{}")
    assert r.status_code == 400


def test_webhook_rejects_wrong_secret(billing_client):
    r = _post_event(
        billing_client,
        _event("customer.subscription.updated", {"customer": "cus_x", "id": "sub_x", "status": "active"}),
        secret="whsec_wrong",
    )
    assert r.status_code == 400


def test_webhook_rejects_tampered_body(billing_client):
    r = _post_event(
        billing_client,
        _event("customer.subscription.updated", {"customer": "cus_x", "id": "sub_x", "status": "active"}),
        tamper=True,
    )
    assert r.status_code == 400


def test_webhook_returns_503_when_secret_not_configured(monkeypatch, tmp_path):
    import importlib
    import sys

    from starlette.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "pingback.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("RESEND_API_KEY", "")
    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    pingback_main = importlib.import_module("pingback.main")
    with TestClient(pingback_main.app) as c:
        r = c.post(WEBHOOK_URL, content=b"{}", headers={"stripe-signature": "t=0,v1=0"})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Plan state transitions
# ---------------------------------------------------------------------------

def test_subscription_active_upgrades_user_to_pro(billing_client):
    user_id = _signup_with_customer(billing_client, "upgrade@example.com", "cus_upgrade")
    assert _user_plan(billing_client, user_id)[0] == "free"

    period_end = int(time.time()) + 30 * 86400
    r = _post_event(
        billing_client,
        _event(
            "customer.subscription.updated",
            {
                "customer": "cus_upgrade",
                "id": "sub_upgrade",
                "status": "active",
                "current_period_end": period_end,
            },
        ),
    )
    assert r.status_code == 200
    plan, sub_id, renews_at = _user_plan(billing_client, user_id)
    assert plan == "pro"
    assert sub_id == "sub_upgrade"
    assert renews_at is not None


def test_subscription_canceled_downgrades_to_free(billing_client):
    user_id = _signup_with_customer(billing_client, "cancel@example.com", "cus_cancel")
    # Start from Pro.
    con = sqlite3.connect(billing_client.db_path)
    con.execute(
        "UPDATE users SET plan = 'pro', stripe_subscription_id = 'sub_cancel', plan_renews_at = '2099-01-01T00:00:00+00:00' WHERE id = ?",
        (user_id,),
    )
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "customer.subscription.deleted",
            {"customer": "cus_cancel", "id": "sub_cancel", "status": "canceled"},
        ),
    )
    assert r.status_code == 200
    plan, sub_id, renews_at = _user_plan(billing_client, user_id)
    assert plan == "free"
    assert sub_id is None
    assert renews_at is None


def test_subscription_past_due_downgrades_to_free(billing_client):
    user_id = _signup_with_customer(billing_client, "pastdue@example.com", "cus_pd")
    r = _post_event(
        billing_client,
        _event(
            "customer.subscription.updated",
            {"customer": "cus_pd", "id": "sub_pd", "status": "past_due"},
        ),
    )
    assert r.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "free"


def test_payment_failed_does_not_change_plan(billing_client):
    user_id = _signup_with_customer(billing_client, "pf@example.com", "cus_pf")
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'pro' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "invoice.payment_failed",
            {"customer": "cus_pf", "id": "in_test"},
        ),
    )
    assert r.status_code == 200
    # Stripe will follow up with subscription.updated if the plan should flip;
    # payment_failed alone must not flip the plan.
    assert _user_plan(billing_client, user_id)[0] == "pro"


def test_checkout_completed_claims_customer_id(billing_client):
    """If subscription.updated arrives before checkout, /billing/checkout already
    set the customer id. Otherwise, checkout.session.completed should claim it."""
    # Sign up without a customer id.
    r = billing_client.post("/signup", data={"email": "claim@example.com"}, follow_redirects=False)
    assert r.status_code == 303
    con = sqlite3.connect(billing_client.db_path)
    row = con.execute(
        "SELECT id FROM users WHERE email_hash = ?", (_hash_email("claim@example.com"),)
    ).fetchone()
    user_id = row[0]
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "checkout.session.completed",
            {
                "customer": "cus_claim",
                "metadata": {"pingback_user_id": user_id},
            },
        ),
    )
    assert r.status_code == 200

    con = sqlite3.connect(billing_client.db_path)
    row = con.execute(
        "SELECT stripe_customer_id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    con.close()
    assert row[0] == "cus_claim"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_duplicate_event_id_does_not_double_flip(billing_client):
    user_id = _signup_with_customer(billing_client, "idem@example.com", "cus_idem")
    event = _event(
        "customer.subscription.updated",
        {"customer": "cus_idem", "id": "sub_idem", "status": "active"},
        event_id="evt_idem_fixed",
    )

    r1 = _post_event(billing_client, event)
    assert r1.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "pro"

    # Flip back manually so we can observe whether the retry re-applies the update.
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'free' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r2 = _post_event(billing_client, event)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    # Retry must NOT re-upgrade the user.
    assert _user_plan(billing_client, user_id)[0] == "free"
