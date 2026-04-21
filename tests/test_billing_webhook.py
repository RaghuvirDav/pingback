"""Paddle webhook: signature verification, plan transitions, idempotency.

These tests sign payloads with the same secret the webhook route reads and
drive the route via the TestClient, so signature verification runs for real.
The Paddle API itself is never called — we only build the JSON payloads that
Paddle would have sent (see https://developer.paddle.com/webhooks/overview).
"""
from __future__ import annotations

import hmac
import json
import sqlite3
import time
import uuid
from hashlib import sha256

import pytest
from cryptography.fernet import Fernet


WEBHOOK_SECRET = "pdl_ntfset_test_secret"
WEBHOOK_URL = "/api/paddle/webhook"


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@pytest.fixture
def billing_app(monkeypatch, tmp_path):
    """Like the shared `app_ctx` but with Paddle credentials populated."""
    import importlib
    import sys

    db_path = tmp_path / "pingback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_test_api")
    monkeypatch.setenv("PADDLE_CLIENT_SIDE_TOKEN", "live_test_client")
    monkeypatch.setenv("PADDLE_NOTIFICATION_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("PADDLE_PRICE_ID_PRO_MONTHLY", "pri_test_pro_monthly")
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


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Build the `Paddle-Signature` header: `ts=<ts>;h1=<hex(hmac_sha256(ts:body))>`."""
    ts = str(int(time.time()))
    signed = f"{ts}:{body.decode()}".encode()
    h1 = hmac.new(secret.encode(), signed, sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def _signup_with_customer(client, email: str, customer_id: str) -> str:
    """Sign up a user and attach a Paddle customer id. Returns the user id."""
    r = client.post("/signup", data={"email": email}, follow_redirects=False)
    assert r.status_code == 303
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


def _user_plan(client, user_id: str) -> tuple[str, str | None, str | None]:
    con = sqlite3.connect(client.db_path)
    row = con.execute(
        "SELECT plan, paddle_subscription_id, plan_renews_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    con.close()
    return row


def _event(event_type: str, data: dict, event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": "2026-04-21T09:00:00Z",
        "notification_id": f"ntf_{uuid.uuid4().hex}",
        "data": data,
    }


def _sub_data(
    *,
    customer_id: str,
    sub_id: str,
    status: str,
    user_id: str | None = None,
    ends_at: str | None = None,
    portal_url: str | None = "https://customer-portal.paddle.com/pdl_test",
) -> dict:
    data: dict = {
        "id": sub_id,
        "status": status,
        "customer_id": customer_id,
    }
    if user_id is not None:
        data["custom_data"] = {"user_id": user_id}
    if ends_at:
        data["current_billing_period"] = {"starts_at": "2026-04-21T00:00:00Z", "ends_at": ends_at}
    if portal_url:
        data["management_urls"] = {"update_payment_method": portal_url, "cancel": portal_url}
    return data


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
        _event("subscription.updated", _sub_data(customer_id="ctm_x", sub_id="sub_x", status="active")),
        secret="pdl_ntfset_wrong",
    )
    assert r.status_code == 400


def test_webhook_rejects_tampered_body(billing_client):
    r = _post_event(
        billing_client,
        _event("subscription.updated", _sub_data(customer_id="ctm_x", sub_id="sub_x", status="active")),
        tamper=True,
    )
    assert r.status_code == 400


def test_webhook_rejects_malformed_header(billing_client):
    body = json.dumps(_event("subscription.updated", _sub_data(customer_id="ctm_x", sub_id="sub_x", status="active"))).encode()
    r = billing_client.post(
        WEBHOOK_URL,
        content=body,
        headers={"paddle-signature": "garbage-no-equals", "content-type": "application/json"},
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
    monkeypatch.setenv("PADDLE_NOTIFICATION_SECRET", "")
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
            _sub_data(
                customer_id="ctm_upgrade",
                sub_id="sub_upgrade",
                status="active",
                user_id=user_id,
                ends_at="2026-05-21T00:00:00Z",
            ),
        ),
    )
    assert r.status_code == 200, r.text
    plan, sub_id, renews_at = _user_plan(billing_client, user_id)
    assert plan == "pro"
    assert sub_id == "sub_upgrade"
    assert renews_at == "2026-05-21T00:00:00Z"


def test_subscription_canceled_downgrades_to_free(billing_client):
    user_id = _signup_with_customer(billing_client, "cancel@example.com", "ctm_cancel")
    con = sqlite3.connect(billing_client.db_path)
    con.execute(
        "UPDATE users SET plan = 'pro', paddle_subscription_id = 'sub_cancel', plan_renews_at = '2099-01-01T00:00:00Z' WHERE id = ?",
        (user_id,),
    )
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "subscription.canceled",
            _sub_data(customer_id="ctm_cancel", sub_id="sub_cancel", status="canceled"),
        ),
    )
    assert r.status_code == 200
    plan, sub_id, renews_at = _user_plan(billing_client, user_id)
    assert plan == "free"
    assert sub_id is None
    assert renews_at is None


def test_subscription_past_due_keeps_user_on_pro(billing_client):
    """Paddle retries failed payments automatically. One failure must not
    silently downgrade a paying user — only `canceled` does that."""
    user_id = _signup_with_customer(billing_client, "pastdue@example.com", "ctm_pd")
    con = sqlite3.connect(billing_client.db_path)
    con.execute(
        "UPDATE users SET plan = 'pro', paddle_subscription_id = 'sub_pd' WHERE id = ?",
        (user_id,),
    )
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "subscription.past_due",
            _sub_data(customer_id="ctm_pd", sub_id="sub_pd", status="past_due"),
        ),
    )
    assert r.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "pro"


def test_transaction_payment_failed_does_not_change_plan(billing_client):
    user_id = _signup_with_customer(billing_client, "pf@example.com", "ctm_pf")
    con = sqlite3.connect(billing_client.db_path)
    con.execute("UPDATE users SET plan = 'pro' WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    r = _post_event(
        billing_client,
        _event(
            "transaction.payment_failed",
            {"id": "txn_pf", "customer_id": "ctm_pf", "subscription_id": "sub_pf"},
        ),
    )
    assert r.status_code == 200
    assert _user_plan(billing_client, user_id)[0] == "pro"


def test_subscription_created_claims_customer_id(billing_client):
    """The first subscription.created webhook for a new user must claim the
    Paddle customer id on the users row via custom_data.user_id."""
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
            "subscription.created",
            _sub_data(
                customer_id="ctm_claim",
                sub_id="sub_claim",
                status="active",
                user_id=user_id,
                ends_at="2026-05-21T00:00:00Z",
            ),
        ),
    )
    assert r.status_code == 200

    con = sqlite3.connect(billing_client.db_path)
    row = con.execute(
        "SELECT paddle_customer_id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    con.close()
    assert row[0] == "ctm_claim"


def test_subscription_created_caches_portal_url(billing_client):
    user_id = _signup_with_customer(billing_client, "portal@example.com", "ctm_portal")
    portal = "https://customer-portal.paddle.com/pdl_abc123"

    r = _post_event(
        billing_client,
        _event(
            "subscription.created",
            _sub_data(
                customer_id="ctm_portal",
                sub_id="sub_portal",
                status="active",
                user_id=user_id,
                ends_at="2026-05-21T00:00:00Z",
                portal_url=portal,
            ),
        ),
    )
    assert r.status_code == 200
    con = sqlite3.connect(billing_client.db_path)
    row = con.execute(
        "SELECT paddle_portal_url FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    con.close()
    assert row[0] == portal


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_duplicate_event_id_does_not_double_flip(billing_client):
    user_id = _signup_with_customer(billing_client, "idem@example.com", "ctm_idem")
    event = _event(
        "subscription.updated",
        _sub_data(
            customer_id="ctm_idem",
            sub_id="sub_idem",
            status="active",
            user_id=user_id,
            ends_at="2026-05-21T00:00:00Z",
        ),
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
