"""JSON API tests (Bearer API key auth)."""
from __future__ import annotations

import re


def _api_key_for(client, email="api@example.com"):
    """Sign up + verify through the UI, then look up the API key in the DB.

    MAK-167: the cookie no longer carries the API key — it carries an opaque
    server-side session id. Tests that need a Bearer token read the user's
    encrypted API key out of the test DB and decrypt it.
    """
    from tests.conftest import api_key_for_email, signup_and_verify

    signup_and_verify(client, email)
    return api_key_for_email(email)


def _user_id(client):
    r = client.get("/dashboard/settings")
    m = re.search(r"/status/([0-9a-f\-]{36})", r.text)
    assert m
    return m.group(1)


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") in ("ok", "healthy", "up")


def test_healthz_pings_db_and_returns_version(client):
    r = client.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["version"]
    assert r.headers.get("X-Pingback-Version") == body["version"]


def test_version_header_on_every_response(client):
    r = client.get("/health")
    assert r.headers.get("X-Pingback-Version")
    r = client.get("/login")
    assert r.headers.get("X-Pingback-Version")


def test_api_requires_bearer_token(client):
    r = client.post("/api/monitors", json={"name": "x", "url": "https://x.com", "interval_seconds": 300})
    assert r.status_code in (401, 403)


def test_api_create_and_list_monitors(client):
    key = _api_key_for(client, email="apikey@example.com")
    headers = {"Authorization": f"Bearer {key}"}

    r = client.post(
        "/api/monitors",
        json={"name": "API Test", "url": "https://example.com", "interval_seconds": 300},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "API Test"
    monitor_id = body["id"]

    uid = _user_id(client)
    r = client.get(f"/api/users/{uid}/monitors", headers=headers)
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()]
    assert monitor_id in ids


def test_api_unauth_monitor_access_forbidden(client):
    key = _api_key_for(client, email="owner@example.com")
    r = client.post(
        "/api/monitors",
        json={"name": "locked", "url": "https://locked.com", "interval_seconds": 300},
        headers={"Authorization": f"Bearer {key}"},
    )
    monitor_id = r.json()["id"]

    # Sign up a second user, use their key to delete the first user's monitor
    client.post("/logout", follow_redirects=False)
    other_key = _api_key_for(client, email="attacker@example.com")

    r = client.delete(f"/monitors/{monitor_id}", headers={"Authorization": f"Bearer {other_key}"})
    assert r.status_code in (403, 404)
