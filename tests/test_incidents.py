"""Dashboard incidents (MAK-113): pill -> #incidents panel with per-monitor errors."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def _signup(client, email="incidents@example.com"):
    from tests.conftest import signup_and_verify
    signup_and_verify(client, email)


def _create_monitor(client, name, url):
    return client.post(
        "/dashboard/monitors/new",
        data={"name": name, "url": url, "interval_seconds": 300, "is_public": 0},
        follow_redirects=False,
    )


def _insert_check(monitor_id: str, status: str, status_code, error):
    """Write a check_result straight into the test SQLite DB."""
    from pingback.config import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO check_results
                   (id, monitor_id, status, status_code, response_time_ms, error, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), monitor_id, status, status_code, 0, error,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def _create_failing_monitor(client, name, url, status, status_code, error):
    r = _create_monitor(client, name=name, url=url)
    monitor_id = r.headers["location"].rsplit("/", 1)[-1]
    _insert_check(monitor_id, status=status, status_code=status_code, error=error)
    return monitor_id


def test_pill_anchors_to_incidents_when_failing(client):
    _signup(client)
    _create_failing_monitor(
        client, "Down site", "https://down.example.com",
        status="down", status_code=None, error="Connection refused",
    )
    r = client.get("/dashboard")
    assert r.status_code == 200
    # Pill links to #incidents (not the old #monitors anchor) when failing.
    assert 'href="#incidents"' in r.text
    assert "1 incident" in r.text
    # The incidents panel renders.
    assert 'id="incidents"' in r.text
    assert "Active incidents" in r.text


def test_incidents_panel_lists_failing_monitors_with_error(client):
    _signup(client)
    _create_failing_monitor(
        client, "API", "https://api.example.com",
        status="down", status_code=503, error="Service Unavailable",
    )
    _create_failing_monitor(
        client, "CV Site", "https://prerna.raghuvir.cv",
        status="error", status_code=None, error="getaddrinfo ENOTFOUND",
    )
    r = client.get("/dashboard")
    assert r.status_code == 200
    # Pill count matches number of incidents shown below.
    assert "2 incidents" in r.text
    # Each failing monitor surfaces in the panel with its error reason.
    assert "API" in r.text
    assert "HTTP 503" in r.text
    assert "Service Unavailable" in r.text
    assert "CV Site" in r.text
    assert "getaddrinfo ENOTFOUND" in r.text
    # And clicking a row drills into the per-monitor detail page.
    assert "/dashboard/monitors/" in r.text


def test_no_incidents_panel_when_all_up(client):
    _signup(client)
    r = _create_monitor(client, name="Healthy", url="https://ok.example.com")
    monitor_id = r.headers["location"].rsplit("/", 1)[-1]
    _insert_check(monitor_id, status="up", status_code=200, error=None)

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "All systems operational" in r.text
    assert 'id="incidents"' not in r.text
    assert "Active incidents" not in r.text


def test_monitor_detail_shows_error_column(client):
    _signup(client)
    monitor_id = _create_failing_monitor(
        client, "Failing", "https://bad.example.com",
        status="error", status_code=None, error="ssl: certificate has expired",
    )
    r = client.get(f"/dashboard/monitors/{monitor_id}")
    assert r.status_code == 200
    # The Recent checks table now exposes an Error column with the failure text.
    assert "<th>Error</th>" in r.text
    assert "ssl: certificate has expired" in r.text
