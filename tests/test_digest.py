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

    # send_hour_utc=8 (interpreted as local hour). 11:00 UTC = 07:00 EDT — not yet due.
    not_due = _due_users(datetime(2026, 4, 28, 11, 30, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in not_due)

    # 12:00 UTC = 08:00 EDT — due.
    due = _due_users(datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc))
    assert any(u["id"] == user_id for u in due)


def test_catch_up_after_missed_hour(client):
    """If the service was down across the user's send hour, they should
    still be eligible later that local day. This is the regression fix
    behind today's missed 08:00 UTC delivery."""
    signup_and_verify(client, "catchup@example.com")
    user_id = _user_id(client, "catchup@example.com")
    _seed_active_monitor(user_id)
    # Etc/UTC user, send_hour_utc=8, never sent.
    # Evaluating at 09:30 UTC on a fresh account: local hour 9 >= 8, last_sent_at NULL.
    due = _due_users(datetime(2026, 4, 28, 9, 30, tzinfo=timezone.utc))
    assert any(u["id"] == user_id for u in due)


def test_skipped_when_already_sent_today(client):
    from pingback.config import DB_PATH

    signup_and_verify(client, "alreadysent@example.com")
    user_id = _user_id(client, "alreadysent@example.com")
    _seed_active_monitor(user_id)

    sent_at = datetime(2026, 4, 28, 8, 1, tzinfo=timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE digest_preferences SET last_sent_at = ? WHERE user_id = ?",
            (sent_at, user_id),
        )
        conn.commit()

    due = _due_users(datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in due)


def test_user_with_no_active_monitors_not_due(client):
    signup_and_verify(client, "nomonitors@example.com")
    user_id = _user_id(client, "nomonitors@example.com")
    # Intentionally no monitors seeded.
    due = _due_users(datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc))
    assert all(u["id"] != user_id for u in due)


def test_settings_post_persists_timezone(client):
    signup_and_verify(client, "tzpicker@example.com")
    r = client.post(
        "/dashboard/settings/notifications",
        data={
            "digest_enabled": "1",
            "send_hour_utc": "8",
            "timezone_name": "Asia/Kolkata",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    user_id = _user_id(client, "tzpicker@example.com")
    row = _db_row(client, "SELECT timezone FROM users WHERE id = ?", user_id)
    assert row["timezone"] == "Asia/Kolkata"

    r = client.get("/dashboard/settings")
    assert 'value="Asia/Kolkata" selected' in r.text


def test_settings_post_rejects_unknown_timezone(client):
    signup_and_verify(client, "badtz@example.com")
    r = client.post(
        "/dashboard/settings/notifications",
        data={
            "digest_enabled": "1",
            "send_hour_utc": "8",
            "timezone_name": "Mars/Olympus_Mons",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Unknown+timezone" in r.headers["location"]


@pytest.mark.asyncio
async def test_send_daily_digests_skips_when_no_resend_key(monkeypatch):
    import pingback.services.email as email_service

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "")
    sent = await email_service.send_daily_digests(datetime.now(timezone.utc))
    assert sent == 0
