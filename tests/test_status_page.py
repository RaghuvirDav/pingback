"""Public status page: visibility, overall status rollup, empty states."""
from __future__ import annotations

import re

from tests.conftest import signup_and_verify


def _user_id(client):
    """Fetch the current logged-in user's id from settings."""
    r = client.get("/dashboard/settings")
    assert r.status_code == 200
    m = re.search(r"/status/([0-9a-f\-]{36})", r.text)
    assert m, "status URL not present on settings page"
    return m.group(1)


def test_public_status_page_shows_only_public_monitors(client):
    signup_and_verify(client, "pub@example.com")
    # Private monitor
    client.post(
        "/dashboard/monitors/new",
        data={"name": "Private API", "url": "https://private.example.com", "interval_seconds": 300, "is_public": 0},
    )
    # Public monitor
    client.post(
        "/dashboard/monitors/new",
        data={"name": "Public Site", "url": "https://public.example.com", "interval_seconds": 300, "is_public": 1},
    )

    uid = _user_id(client)
    r = client.get(f"/status/{uid}")
    assert r.status_code == 200
    assert "Public Site" in r.text
    # Private monitor must not leak
    assert "Private API" not in r.text


def test_status_page_unknown_user_is_404(client):
    r = client.get("/status/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_status_page_empty_state(client):
    """User exists but has no public monitors — should render gracefully."""
    signup_and_verify(client, "empty@example.com")
    uid = _user_id(client)
    r = client.get(f"/status/{uid}")
    assert r.status_code == 200
    assert "Service status" in r.text
