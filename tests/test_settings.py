"""Settings page + notification + rotate-key flows (integration)."""
from __future__ import annotations


def test_settings_page_renders(client):
    client.post("/signup", data={"email": "settings@example.com"}, follow_redirects=False)
    r = client.get("/dashboard/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    assert "settings@example.com" in r.text
    assert "Daily digest" in r.text


def test_notification_preferences_persist(client):
    client.post("/signup", data={"email": "notifs@example.com"}, follow_redirects=False)
    r = client.post(
        "/dashboard/settings/notifications",
        data={"digest_enabled": "1", "send_hour_utc": "17"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get("/dashboard/settings")
    # The form uses <select> — verify 17 option is selected.
    assert 'value="17" selected' in r.text


def test_rotate_api_key_via_ui(client):
    client.post("/signup", data={"email": "rotate@example.com"}, follow_redirects=False)
    r = client.post("/dashboard/settings/rotate-key", follow_redirects=False)
    assert r.status_code == 303


def test_status_url_displayed_on_settings_page(client):
    client.post("/signup", data={"email": "status@example.com"}, follow_redirects=False)
    r = client.get("/dashboard/settings")
    assert "/status/" in r.text
