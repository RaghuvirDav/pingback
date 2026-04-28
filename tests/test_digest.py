"""Daily digest eligibility + scheduling (MAK-124).

Covers the timezone-aware `get_users_due_for_digest` query, the catch-up
behaviour for missed hour boundaries, the consent backfill on signup, and
the dashboard timezone picker round-trip.
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import signup_and_verify


def _db_row(client, sql: str, *args):
    from pingback.config import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchone()


def _user_id(client, email: str) -> str:
    from pingback.auth import hash_email
    from pingback.config import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM users WHERE email_hash = ?", (hash_email(email),)
        ).fetchone()
    return row["id"]


def _set_user_tz(user_id: str, tz: str) -> None:
    from pingback.config import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET timezone = ? WHERE id = ?", (tz, user_id))
        conn.commit()


def _seed_active_monitor(user_id: str) -> None:
    from pingback.config import DB_PATH

    monitor_id = str(uuid.uuid4())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO monitors (id, user_id, name, url, interval_seconds, status)
               VALUES (?, ?, 'Test', 'https://example.com', 300, 'active')""",
            (monitor_id, user_id),
        )
        conn.commit()


def _due_users(at_utc: datetime) -> list[dict]:
    from pingback.db.connection import get_database
    from pingback.db.digest import get_users_due_for_digest

    async def _run():
        db = await get_database()
        return await get_users_due_for_digest(db, at_utc)

    return asyncio.run(_run())


def test_signup_sets_consent_and_default_timezone(client):
    """Signup must record consent + a default timezone — the missing consent
    flag was the root cause of MAK-124 (every user filtered out)."""
    signup_and_verify(client, "consent@example.com")
    row = _db_row(client, "SELECT consent_given_at, timezone FROM users WHERE email_hash = ?",
                 __import__("pingback.auth", fromlist=["hash_email"]).hash_email("consent@example.com"))
    assert row["consent_given_at"] is not None
    assert row["timezone"] == "Etc/UTC"


def test_due_user_in_local_tz_only_at_local_send_hour(client):
    signup_and_verify(client, "tz@example.com")
    user_id = _user_id(client, "tz@example.com")
    _seed_active_monitor(user_id)
    _set_user_tz(user_id, "America/New_York")  # UTC-4 in April (EDT)

    # 11:30 UTC = 07:30 EDT — outside the ±7 min window around 08:00 local.
    not_due = _due_users(datetime(2026, 4, 28, 11, 30, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in not_due)

    # 12:00 UTC = 08:00 EDT — inside the window.
    due = _due_users(datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc))
    assert any(u["id"] == user_id for u in due)

    # 12:05 UTC = 08:05 EDT — still inside the ±7 min window.
    due_edge = _due_users(datetime(2026, 4, 28, 12, 5, tzinfo=timezone.utc))
    assert any(u["id"] == user_id for u in due_edge)

    # 12:15 UTC = 08:15 EDT — outside the window again.
    past = _due_users(datetime(2026, 4, 28, 12, 15, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in past)


def test_match_window_is_narrow_no_catch_up(client):
    """MAK-126: with the ±7 min window the scheduler no longer catches up
    on missed sends — the spec accepts that in exchange for ≤15 min latency
    when the scheduler is healthy."""
    signup_and_verify(client, "catchup@example.com")
    user_id = _user_id(client, "catchup@example.com")
    _seed_active_monitor(user_id)
    # Etc/UTC user, never sent. 09:30 UTC is well past 08:00 ± 7 min.
    due = _due_users(datetime(2026, 4, 28, 9, 30, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in due)


def test_skipped_when_already_sent_today(client):
    from pingback.config import DB_PATH

    signup_and_verify(client, "alreadysent@example.com")
    user_id = _user_id(client, "alreadysent@example.com")
    _seed_active_monitor(user_id)

    # Sent earlier today (07:55 UTC). Re-evaluate inside the 08:00 ± 7 min
    # window — user must NOT be re-matched even though the time window fits.
    sent_at = datetime(2026, 4, 28, 7, 55, tzinfo=timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE digest_preferences SET last_sent_at = ? WHERE user_id = ?",
            (sent_at, user_id),
        )
        conn.commit()

    due = _due_users(datetime(2026, 4, 28, 8, 0, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in due)


def test_user_with_no_active_monitors_not_due(client):
    signup_and_verify(client, "nomonitors@example.com")
    user_id = _user_id(client, "nomonitors@example.com")
    # Intentionally no monitors seeded. Evaluate inside the match window.
    due = _due_users(datetime(2026, 4, 28, 8, 0, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in due)


def test_billing_post_persists_timezone(client):
    signup_and_verify(client, "tzpicker@example.com")
    r = client.post(
        "/dashboard/settings/notifications",
        data={
            "digest_enabled": "1",
            "timezone_name": "Asia/Kolkata",
            "redirect_to": "/dashboard/billing",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/dashboard/billing")

    user_id = _user_id(client, "tzpicker@example.com")
    row = _db_row(client, "SELECT timezone FROM users WHERE id = ?", user_id)
    assert row["timezone"] == "Asia/Kolkata"

    r = client.get("/dashboard/billing")
    assert 'value="Asia/Kolkata" selected' in r.text


def test_notifications_post_rejects_unknown_timezone(client):
    signup_and_verify(client, "badtz@example.com")
    r = client.post(
        "/dashboard/settings/notifications",
        data={
            "digest_enabled": "1",
            "timezone_name": "Mars/Olympus_Mons",
            "redirect_to": "/dashboard/billing",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Unknown+timezone" in r.headers["location"]


def test_api_users_me_timezone_seeds_default(client):
    signup_and_verify(client, "seedtz@example.com")
    user_id = _user_id(client, "seedtz@example.com")
    # User is Etc/UTC by default; browser-detect should overwrite.
    r = client.post("/api/users/me/timezone", json={"timezone": "Asia/Kolkata"})
    assert r.status_code == 200
    assert r.json() == {"updated": True, "timezone": "Asia/Kolkata"}
    row = _db_row(client, "SELECT timezone FROM users WHERE id = ?", user_id)
    assert row["timezone"] == "Asia/Kolkata"


def test_api_users_me_timezone_does_not_clobber_explicit_pick(client):
    signup_and_verify(client, "keeptz@example.com")
    user_id = _user_id(client, "keeptz@example.com")
    _set_user_tz(user_id, "Europe/Berlin")
    r = client.post("/api/users/me/timezone", json={"timezone": "Asia/Kolkata"})
    assert r.status_code == 200
    assert r.json()["updated"] is False
    row = _db_row(client, "SELECT timezone FROM users WHERE id = ?", user_id)
    assert row["timezone"] == "Europe/Berlin"


def test_api_users_me_timezone_rejects_unknown(client):
    signup_and_verify(client, "badseed@example.com")
    r = client.post("/api/users/me/timezone", json={"timezone": "Mars/Olympus_Mons"})
    assert r.status_code == 400


def test_api_users_me_timezone_requires_auth(client):
    client.cookies.clear()
    r = client.post("/api/users/me/timezone", json={"timezone": "Asia/Kolkata"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_daily_digests_skips_when_no_resend_key(monkeypatch):
    import pingback.services.email as email_service

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "")
    sent = await email_service.send_daily_digests(datetime.now(timezone.utc))
    assert sent == 0
