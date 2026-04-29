"""JSON API tests (Bearer API key auth)."""
from __future__ import annotations

import re


def _api_key_for(client, email="api@example.com"):
    """Sign up + verify through the UI, then decode the session cookie to recover the API key.

    The API key is stored in a signed `pb_session` cookie (base64-encoded,
    HMAC-signed). We decode it here so tests can exercise the JSON API with a
    real Bearer token.
    """
    from pingback.session import _verify
    from tests.conftest import signup_and_verify

    signup_and_verify(client, email)
    cookie = client.cookies.get("pb_session")
    assert cookie, "pb_session cookie not set after verification"
    # Some cookie jars surround values with double quotes when they contain
    # `=`. Normalise before HMAC verification.
    cookie = cookie.strip('"')
    api_key = _verify(cookie)
    assert api_key, f"pb_session cookie failed signature verification: {cookie!r}"
    return api_key


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
