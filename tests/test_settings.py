"""Settings page + notification + rotate-key flows (integration)."""
from __future__ import annotations

from tests.conftest import signup_and_verify


def test_settings_page_renders(client):
    signup_and_verify(client, "settings@example.com")
    r = client.get("/dashboard/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    assert "settings@example.com" in r.text
    # MAK-126 moved digest controls to the Billing page; settings now only
    # links over to /dashboard/billing.
    assert "/dashboard/billing" in r.text


def test_notification_preferences_persist(client):
    signup_and_verify(client, "notifs@example.com")
    r = client.post(
        "/dashboard/settings/notifications",
        data={"digest_enabled": "1", "timezone_name": "Asia/Kolkata"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Picker now lives on billing.
    r = client.get("/dashboard/billing")
    assert 'value="Asia/Kolkata" selected' in r.text


def test_rotate_api_key_via_ui(client):
    signup_and_verify(client, "rotate@example.com")
    r = client.post("/dashboard/settings/rotate-key", follow_redirects=False)
    assert r.status_code == 303


def test_status_url_displayed_on_settings_page(client):
    signup_and_verify(client, "status@example.com")
    r = client.get("/dashboard/settings")
    assert "/status/" in r.text
