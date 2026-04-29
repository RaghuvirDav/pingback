"""Black-box tests: every user-facing page renders with the new design system."""
from __future__ import annotations


def test_landing_page_renders_with_new_design(client):
    r = client.get("/")
    assert r.status_code == 200
    # New design tokens
    assert "/static/app.css" in r.text
    assert "Uptime monitoring" in r.text
    # Old Tailwind CDN must be gone
    assert "cdn.tailwindcss.com" not in r.text
    assert "hero-demo-frame" in r.text


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Welcome back" in r.text
    assert "Email" in r.text
    assert "Password" in r.text
    assert "Forgot password?" in r.text


def test_signup_page_renders(client):
    r = client.get("/signup")
    assert r.status_code == 200
    assert "Create your account" in r.text


def test_404_is_custom(client):
    r = client.get("/this-page-does-not-exist")
    assert r.status_code == 404
    assert "Page not found" in r.text
    assert "/static/app.css" in r.text


def test_static_css_served(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "--accent" in r.text
    assert "Geist" in r.text or "ff-sans" in r.text


def test_dashboard_requires_login(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


def test_dashboard_has_role_main_and_aria_current(client):
    """A11y: dashboard page must expose the main landmark and mark the
    active sidebar item with aria-current."""
    from tests.conftest import signup_and_verify
    signup_and_verify(client, "a11y@example.com")
    r = client.get("/dashboard")
    assert r.status_code == 200
    # MAK-164: now uses the <main> element (implicit role=main) + skip link.
    assert '<main' in r.text and 'id="main"' in r.text
    assert 'class="skip-link"' in r.text
    # Overview is the active route here.
    assert 'aria-current="page"' in r.text
    # The active marker should appear on the Overview link, not Settings.
    overview_idx = r.text.find(">Overview<")
    settings_idx = r.text.find(">Settings<")
    aria_idx = r.text.find('aria-current="page"')
    assert overview_idx != -1 and aria_idx != -1
    assert aria_idx < overview_idx, "aria-current should be on Overview link"
    # And if we move to settings, aria-current should follow.
    r = client.get("/dashboard/settings")
    assert r.status_code == 200
    assert 'aria-current="page"' in r.text
    settings_idx = r.text.find(">Settings<")
    aria_idx = r.text.find('aria-current="page"')
    assert aria_idx < settings_idx


def test_landing_has_role_main(client):
    r = client.get("/")
    # MAK-164: <main> element provides the implicit main landmark.
    assert '<main' in r.text and 'id="main"' in r.text


def test_incidents_pill_is_clickable_when_monitor_down(client):
    """The hero `N incidents` pill must be a link that filters to failing monitors."""
    import asyncio

    from tests.conftest import signup_and_verify

    signup_and_verify(client, "incidents@example.com")
    r = client.post(
        "/dashboard/monitors/new",
        data={"name": "Down Site", "url": "https://broken.example.com", "interval_seconds": 300, "is_public": 0},
        follow_redirects=False,
    )
    assert r.status_code == 303
    monitor_id = r.headers["location"].rsplit("/", 1)[-1]

    from pingback.db.connection import get_database
    from pingback.db.monitors import save_check_result

    async def _seed_down():
        db = await get_database()
        await save_check_result(db, monitor_id, "down", 503, 1234, "service unavailable")

    asyncio.run(_seed_down())

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'class="status-pill is-link"' in r.text
    assert 'href="?filter=down#monitors"' in r.text
    assert "1 incident" in r.text
    assert 'data-status="down"' in r.text


def test_dashboard_first_run_does_not_fabricate_uptime_or_heatmap(client):
    """Brand-new account, no monitors yet → empty state, no green heatmap."""
    from tests.conftest import signup_and_verify

    signup_and_verify(client, "first-run@example.com")
    r = client.get("/dashboard")
    assert r.status_code == 200
    # No monitors yet → empty-state path renders, the heatmap and its placeholder
    # are gated on having monitors so neither should appear.
    assert "Building 90-day history" not in r.text
    assert "heatmap" not in r.text or "No monitors yet" in r.text
    assert "100.0%" not in r.text


def test_dashboard_with_monitor_no_checks_shows_pending_heatmap(client):
    """Monitor exists but no checks have run → placeholder, never a green wall."""
    from tests.conftest import signup_and_verify

    signup_and_verify(client, "pending-heatmap@example.com")
    r = client.post(
        "/dashboard/monitors/new",
        data={"name": "Fresh", "url": "https://example.com", "interval_seconds": 300, "is_public": 0},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.get("/dashboard")
    assert r.status_code == 200
    # The placeholder must render; the cell-grid must not (otherwise the
    # 90 default-green cells reappear).
    assert "Building 90-day history" in r.text
    assert 'class="cell ' not in r.text
    # The hero "Uptime · 30d" stat should fall back to em-dash, not 100.0%.
    assert "100.0%" not in r.text


def test_monitor_detail_first_run_renders_awaiting_first_check(client):
    """Monitor detail before any check runs: em-dash uptime + awaiting state."""
    from tests.conftest import signup_and_verify

    signup_and_verify(client, "monitor-detail-first@example.com")
    r = client.post(
        "/dashboard/monitors/new",
        data={"name": "Fresh Monitor", "url": "https://example.com", "interval_seconds": 300, "is_public": 0},
        follow_redirects=False,
    )
    assert r.status_code == 303
    monitor_id = r.headers["location"].rsplit("/", 1)[-1]

    r = client.get(f"/dashboard/monitors/{monitor_id}")
    assert r.status_code == 200
    assert "Awaiting first check" in r.text
    # No fabricated 100% uptime or "healthy" badge before the first check
    assert "100.0%" not in r.text
    assert "▲ healthy" not in r.text


def test_monitor_detail_after_first_check_shows_uptime(client):
    """Once a check has been recorded, the real uptime KPI returns."""
    import asyncio

    from tests.conftest import signup_and_verify

    signup_and_verify(client, "monitor-detail-history@example.com")
    r = client.post(
        "/dashboard/monitors/new",
        data={"name": "Has Checks", "url": "https://example.com", "interval_seconds": 300, "is_public": 0},
        follow_redirects=False,
    )
    assert r.status_code == 303
    monitor_id = r.headers["location"].rsplit("/", 1)[-1]

    from pingback.db.connection import get_database
    from pingback.db.monitors import save_check_result

    async def _seed_up():
        db = await get_database()
        await save_check_result(db, monitor_id, "up", 200, 123, None)

    asyncio.run(_seed_up())

    r = client.get(f"/dashboard/monitors/{monitor_id}")
    assert r.status_code == 200
    assert "Awaiting first check" not in r.text
    assert "100.0%" in r.text


def test_terms_page_renders(client):
    r = client.get("/terms")
    assert r.status_code == 200
    assert "Terms of Service" in r.text
    assert "Last updated:" in r.text
    assert "/static/app.css" in r.text


def test_privacy_page_renders(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "Privacy Policy" in r.text
    assert "Last updated:" in r.text


def test_refund_page_renders(client):
    r = client.get("/refund")
    assert r.status_code == 200
    assert "Refund Policy" in r.text
    assert "14-day no-questions refund" in r.text
    # Uniform worldwide policy — no EU/UK carveout
    assert "EU / UK" not in r.text
    assert "cooling-off" not in r.text


def test_landing_footer_links_legal_pages(client):
    r = client.get("/")
    assert 'href="/terms"' in r.text
    assert 'href="/privacy"' in r.text
    assert 'href="/refund"' in r.text


def test_pricing_footer_links_legal_pages(client):
    r = client.get("/pricing")
    assert 'href="/terms"' in r.text
    assert 'href="/privacy"' in r.text
    assert 'href="/refund"' in r.text
