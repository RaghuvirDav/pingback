"""Paddle webhook: signature verification, plan transitions, idempotency.

These tests sign payloads with the same secret the webhook route reads and
drive the route via the TestClient, so signature verification runs for real.
The Paddle API itself is never called — we only build the JSON payloads that
Paddle would have sent.

Paddle-Signature scheme:
    Header value:    ts=<unix>;h1=<hex>
    Signed payload:  f"{ts}:{raw_body}"
    Algorithm:       HMAC-SHA256 with PADDLE_WEBHOOK_SECRET
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid

import pytest
from cryptography.fernet import Fernet


WEBHOOK_SECRET = "pdl_ntfset_test_secret"
WEBHOOK_URL = "/api/paddle/webhook"


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@pytest.fixture
def billing_app(monkeypatch, tmp_path):
    import importlib
    import sys

    db_path = tmp_path / "pingback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("PADDLE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_test_apikey")
    monkeypatch.setenv("PADDLE_CLIENT_TOKEN", "test_client_token")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("PADDLE_PRODUCT_ID", "pro_test")
    monkeypatch.setenv("PADDLE_PRICE_ID_MONTHLY", "pri_test_monthly")
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
    signed = f"{ts}:{payload.decode()}".encode("utf-8")
    h1 = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def _signup_with_customer(client, email: str, customer_id: str) -> str:
    """Sign up a user and attach a Paddle customer id. Returns the user id."""
    from tests.conftest import signup_and_verify
    signup_and_verify(client, email)
    con = sqlite3.connect(client.db_path)
    row = con.execute(
        "SELECT id FROM users WHERE email_hash = ?",
        (_hash_email(email),),
    ).fetchone()
    assert row, "signup did not create a user"
    user_id = row[0]
    con.execute(
        "UPDATE users SET paddle_customer_id = ? WHERE id = ?",
        (customer_id, user_id),
    )
    con.commit()
    con.close()
    return user_id


def _hash_email(email: str) -> str:
    from pingback.auth import hash_email

    return hash_email(email)


def _user_plan(client, user_id: str):
    con = sqlite3.connect(client.db_path)
    row = con.execute(
        "SELECT plan, paddle_subscription_id, plan_renews_at, plan_cancel_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    con.close()
    return row


def _event(event_type: str, data: dict, event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": "2026-04-21T10:00:00Z",
        "notification_id": f"ntf_{uuid.uuid4().hex}",
        "data": data,
    }


def _post_event(client, event: dict, *, secret: str = WEBHOOK_SECRET, tamper: bool = False):
    body = json.dumps(event).encode()
    sig = _sign(body, secret=secret)
    if tamper:
        body = body + b" "  # payload drifts from the signature
    return client.post(
        WEBHOOK_URL,
        content=body,
        headers={"paddle-signature": sig, "content-type": "application/json"},
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
        _event("subscription.updated", {"customer_id": "ctm_x", "id": "sub_x", "status": "active"}),
        secret="pdl_ntfset_wrong",
    )
    assert r.status_code == 400


def test_webhook_rejects_tampered_body(billing_client):
    r = _post_event(
        billing_client,
        _event("subscription.updated", {"customer_id": "ctm_x", "id": "sub_x", "status": "active"}),
        tamper=True,
    )
    assert r.status_code == 400


def test_webhook_rejects_malformed_signature_header(billing_client):
    body = json.dumps(_event("subscription.updated", {"customer_id": "ctm_x"})).encode()
    r = billing_client.post(WEBHOOK_URL, content=body, headers={"paddle-signature": "garbage"})
    assert r.status_code == 400


def test_webhook_returns_503_when_secret_not_configured(monkeypatch, tmp_path):
    import importlib
    import sys

    from starlette.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "pingback.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("RESEND_API_KEY", "")
    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    pingback_main = importlib.import_module("pingback.main")
    with TestClient(pingback_main.app) as c:
        r = c.post(WEBHOOK_URL, content=b"{}", headers={"paddle-signature": "ts=0;h1=0"})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Plan state transitions
# ---------------------------------------------------------------------------

def test_subscription_active_upgrades_user_to_pro(billing_client):
    user_id = _signup_with_customer(billing_client, "upgrade@example.com", "ctm_upgrade")
    assert _user_plan(billing_client, user_id)[0] == "free"

    r = _post_event(
        billing_client,
        _event(
            "subscription.updated",
            {
                "customer_id": "ctm_upgrade",
                "id": "sub_upgrade",
                "status": "active",
                "next_billed_at": "2026-05-21T00:00:00Z",
            },
        ),
    )
    assert r.status_code == 200
    plan, sub_id, renews_at, cancel_at = _user_plan(billing_client, user_id)
    assert plan == "pro"
    assert sub_id == "sub_upgrade"
    assert renews_at == "2026-05-21T00:00:00Z"
    assert cancel_at is None


def test_subscription_canceled_immediate_downgrades_to_free(billing_client):
    user_id = _signup_with_customer(billing_client, "cancel@example.com", "ctm_cancel")
    con = sqlite3.connect(billing_client.db_path)
    con.execute(
        "UPDATE users SET plan = 'pro', paddle_subscription_id = 'sub_cancel' WHERE id = ?",
        (user_id,),
    )
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "subscription.canceled",
            {"customer_id": "ctm_cancel", "id": "sub_cancel", "status": "canceled"},
        ),
    )
    assert r.status_code == 200
    plan, sub_id, renews_at, cancel_at = _user_plan(billing_client, user_id)
    assert plan == "free"
    assert sub_id is None
    assert renews_at is None
    assert cancel_at is None


def test_subscription_canceled_with_scheduled_change_keeps_pro_until_effective(billing_client):
    """Paddle keeps the user on Pro until scheduled_change.effective_at; we stamp
    plan_cancel_at and leave plan='pro'."""
    user_id = _signup_with_customer(billing_client, "sched@example.com", "ctm_sched")
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'pro' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "subscription.updated",
            {
                "customer_id": "ctm_sched",
                "id": "sub_sched",
                "status": "active",
                "scheduled_change": {
                    "action": "cancel",
                    "effective_at": "2026-06-01T00:00:00Z",
                },
            },
        ),
    )
    assert r.status_code == 200
    plan, _sub, _renews, cancel_at = _user_plan(billing_client, user_id)
    assert plan == "pro"
    assert cancel_at == "2026-06-01T00:00:00Z"


def test_subscription_past_due_keeps_user_on_pro(billing_client):
    """past_due means Paddle is still retrying; the user shouldn't lose access
    yet. Only canceled/expired events flip plan to free."""
    user_id = _signup_with_customer(billing_client, "pd@example.com", "ctm_pd")
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'pro' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "subscription.updated",
            {"customer_id": "ctm_pd", "id": "sub_pd", "status": "past_due"},
        ),
    )
    assert r.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "pro"


def test_payment_failed_does_not_change_plan(billing_client):
    user_id = _signup_with_customer(billing_client, "pf@example.com", "ctm_pf")
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'pro' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "transaction.payment_failed",
            {"customer_id": "ctm_pf", "id": "txn_pf"},
        ),
    )
    assert r.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "pro"


def test_subscription_created_claims_customer_id_via_custom_data(billing_client):
    """The Paddle.js overlay sends pingback_user_id in custom_data so the very
    first subscription.created event can attach the customer to the local user
    without any prior /billing/checkout server call."""
    from tests.conftest import signup_and_verify
    signup_and_verify(billing_client, "claim@example.com")
    con = sqlite3.connect(billing_client.db_path)
    row = con.execute(
        "SELECT id FROM users WHERE email_hash = ?", (_hash_email("claim@example.com"),)
    ).fetchone()
    user_id = row[0]
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "subscription.created",
            {
                "customer_id": "ctm_claim",
                "id": "sub_claim",
                "status": "active",
                "next_billed_at": "2026-05-21T00:00:00Z",
                "custom_data": {"pingback_user_id": user_id},
            },
        ),
    )
    assert r.status_code == 200

    con = sqlite3.connect(billing_client.db_path)
    row = con.execute(
        "SELECT plan, paddle_customer_id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    con.close()
    assert row[0] == "pro"
    assert row[1] == "ctm_claim"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_duplicate_event_id_does_not_double_flip(billing_client):
    user_id = _signup_with_customer(billing_client, "idem@example.com", "ctm_idem")
    event = _event(
        "subscription.updated",
        {"customer_id": "ctm_idem", "id": "sub_idem", "status": "active"},
        event_id="evt_idem_fixed",
    )

    r1 = _post_event(billing_client, event)
    assert r1.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "pro"

    # Manually flip back so we can detect whether the retry re-applies.
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'free' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r2 = _post_event(billing_client, event)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    assert _user_plan(billing_client, user_id)[0] == "free"
