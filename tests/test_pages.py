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
    assert "API key" in r.text


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
