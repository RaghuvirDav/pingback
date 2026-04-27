"""Server-side feature gating: monitor quota, interval floor, retention.

These assert the decisions we advertise on the pricing page actually hold at
the API/dashboard boundary — a client can't edit HTML forms to cheat past
them.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from pingback.services.plans import (
    PlanLimitExceeded,
    ensure_interval_allowed,
    ensure_monitor_quota,
    limits_for,
)


# ---------------------------------------------------------------------------
# Pure decision helpers
# ---------------------------------------------------------------------------

def test_free_plan_monitor_quota():
    # 2 existing monitors ok; 3 is the cap.
    ensure_monitor_quota("free", 2)
    with pytest.raises(PlanLimitExceeded):
        ensure_monitor_quota("free", 3)


def test_pro_plan_monitor_quota():
    ensure_monitor_quota("pro", 19)
    with pytest.raises(PlanLimitExceeded):
        ensure_monitor_quota("pro", 20)


def test_business_plan_monitor_quota():
    ensure_monitor_quota("business", 99)
    with pytest.raises(PlanLimitExceeded):
        ensure_monitor_quota("business", 100)


def test_free_plan_interval_floor():
    ensure_interval_allowed("free", 300)
    ensure_interval_allowed("free", 600)
    with pytest.raises(PlanLimitExceeded):
        ensure_interval_allowed("free", 60)


def test_pro_plan_interval_floor():
    ensure_interval_allowed("pro", 60)
    with pytest.raises(PlanLimitExceeded):
        ensure_interval_allowed("pro", 30)


def test_limits_fallback_to_free_for_unknown_plan():
    assert limits_for(None).max_monitors == 3
    assert limits_for("enterprise").min_interval_seconds == 300


def test_history_retention_per_plan():
    assert limits_for("free").history_days == 7
    assert limits_for("pro").history_days == 90
    assert limits_for("business").history_days == 365


# ---------------------------------------------------------------------------
# API enforcement — free user cannot set a sub-5-min interval or add a 6th
# ---------------------------------------------------------------------------

def _api_key(client) -> str:
    from pingback.auth import hash_email

    con = sqlite3.connect(os.environ["DB_PATH"])
    row = con.execute(
        "SELECT api_key FROM users WHERE email_hash = ?",
        (hash_email(client.email),),
    ).fetchone()
    con.close()
    assert row, "user row missing"
    # api_key is Fernet-encrypted at rest.
    from pingback.encryption import decrypt_value

    return decrypt_value(row[0])


def test_free_user_api_cannot_set_60s_interval(auth_client):
    api_key = _api_key(auth_client)
    r = auth_client.post(
        "/api/monitors",
        json={"name": "m", "url": "https://example.com", "interval_seconds": 60},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 403
    assert "minimum" in r.json()["detail"].lower()


def test_free_user_api_blocked_past_quota(auth_client):
    api_key = _api_key(auth_client)
    headers = {"Authorization": f"Bearer {api_key}"}
    for i in range(3):
        r = auth_client.post(
            "/api/monitors",
            json={"name": f"m{i}", "url": "https://example.com", "interval_seconds": 300},
            headers=headers,
        )
        assert r.status_code == 201, r.text
    # 4th must be blocked.
    r = auth_client.post(
        "/api/monitors",
        json={"name": "m4", "url": "https://example.com", "interval_seconds": 300},
        headers=headers,
    )
    assert r.status_code == 403
    assert "upgrade" in r.json()["detail"].lower()


def test_pro_user_api_can_use_60s_interval(auth_client):
    # Upgrade to pro directly.
    con = sqlite3.connect(os.environ["DB_PATH"])
    con.execute("UPDATE users SET plan = 'pro'")
    con.commit()
    con.close()

    api_key = _api_key(auth_client)
    r = auth_client.post(
        "/api/monitors",
        json={"name": "m", "url": "https://example.com", "interval_seconds": 60},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Dashboard (form) enforcement
# ---------------------------------------------------------------------------

def test_free_user_form_cannot_create_sub_5min_monitor(auth_client):
    r = auth_client.post(
        "/dashboard/monitors/new",
        data={"name": "m", "url": "https://example.com", "interval_seconds": 60, "is_public": 0},
    )
    assert r.status_code == 403
    assert "minimum" in r.text.lower()
