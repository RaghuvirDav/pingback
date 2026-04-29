"""Public status page: visibility, overall status rollup, slug + redirect (MAK-163)."""
from __future__ import annotations

import re
import sqlite3

from tests.conftest import signup_and_verify


_GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _slug_url(client) -> str:
    """Return the /status/<slug> URL the settings page advertises."""
    r = client.get("/dashboard/settings")
    assert r.status_code == 200
    m = re.search(r'value="(http[^"]+/status/[^"]+)"', r.text)
    assert m, "status URL not present on settings page"
    return m.group(1)


def _slug_path(client) -> str:
    """Return just the path portion: /status/<slug>."""
    return "/status/" + _slug_url(client).rsplit("/", 1)[-1]


def _user_id(client) -> str:
    """Look up the logged-in user's GUID directly from the test DB."""
    from pingback.auth import hash_email
    from pingback.config import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # Last user inserted is the one our session belongs to (one-per-test DB).
        row = conn.execute(
            "SELECT id FROM users ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return row["id"]


# ---------------------------------------------------------------------------
# Original behaviour: visibility + 404 + empty state. Now resolved by slug.
# ---------------------------------------------------------------------------

def test_public_status_page_shows_only_public_monitors(client):
    signup_and_verify(client, "pub@example.com", name="Public Co")
    client.post(
        "/dashboard/monitors/new",
        data={"name": "Private API", "url": "https://private.example.com",
              "interval_seconds": 300, "is_public": 0},
    )
    client.post(
        "/dashboard/monitors/new",
        data={"name": "Public Site", "url": "https://public.example.com",
              "interval_seconds": 300, "is_public": 1},
    )

    r = client.get(_slug_path(client))
    assert r.status_code == 200
    assert "Public Site" in r.text
    assert "Private API" not in r.text


def test_status_page_unknown_slug_or_id_is_404(client):
    r = client.get("/status/nope-not-a-real-slug")
    assert r.status_code == 404
    r = client.get("/status/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_status_page_empty_state(client):
    signup_and_verify(client, "empty@example.com", name="Empty Inc")
    r = client.get(_slug_path(client))
    assert r.status_code == 200
    assert "status" in r.text.lower()


# ---------------------------------------------------------------------------
# MAK-163: slug + readable title.
# ---------------------------------------------------------------------------

def test_settings_advertises_slug_url_not_guid(client):
    """Settings page MUST display the slug URL — never the raw GUID — so users
    share the branded path."""
    signup_and_verify(client, "acme@example.com", name="Acme Corp")
    url = _slug_url(client)
    assert "/status/" in url
    assert not _GUID_RE.search(url), f"settings URL still leaks a GUID: {url}"


def test_status_page_title_contains_account_name(client):
    """`<title>` should be `<name> status — Pingback`, not the GUID."""
    signup_and_verify(client, "acme@example.com", name="Acme Corp")
    r = client.get(_slug_path(client))
    assert r.status_code == 200
    title_match = re.search(r"<title>([^<]+)</title>", r.text)
    assert title_match
    title = title_match.group(1)
    assert "Acme Corp" in title
    assert "Pingback" in title
    assert not _GUID_RE.search(title)


def test_guid_url_redirects_to_slug(client):
    """Old `/status/<guid>` links keep working but 302 to the canonical slug
    so SEO + share previews use the branded URL."""
    signup_and_verify(client, "redir@example.com", name="Redir Co")
    user_id = _user_id(client)
    canonical = _slug_path(client)

    r = client.get(f"/status/{user_id}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == canonical


def test_two_users_with_same_name_get_distinct_slugs(app_ctx):
    """Slug uniqueness must hold even when two accounts share a display name."""
    from starlette.testclient import TestClient

    from tests.conftest import install_csrf_autoinject

    # First user gets "acme-corp"; second collides and falls through to a
    # disambiguated form (`acme-corp-<userid-prefix>`).
    with TestClient(app_ctx.app) as c1:
        install_csrf_autoinject(c1)
        signup_and_verify(c1, "first@example.com", name="Acme Corp")
        first_url = _slug_url(c1)

    with TestClient(app_ctx.app) as c2:
        install_csrf_autoinject(c2)
        signup_and_verify(c2, "second@example.com", name="Acme Corp")
        second_url = _slug_url(c2)

    assert first_url != second_url
    assert first_url.endswith("/acme-corp")
    assert "/acme-corp-" in second_url  # disambiguated suffix


def test_user_can_change_slug(client):
    signup_and_verify(client, "slugchange@example.com", name="Old Name")
    r = client.post(
        "/dashboard/settings/status-page-slug",
        data={"slug": "my-cool-page"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "success" in r.headers["location"]

    r = client.get("/status/my-cool-page")
    assert r.status_code == 200


def test_slug_form_rejects_invalid_input(client):
    signup_and_verify(client, "invalid@example.com", name="Inv Co")
    # Mixed-case is forgiven (the handler lowercases user input). The rest are
    # genuinely invalid and must be rejected.
    for bad in ["", "ab", "has space", "trailing-", "-leading", "under_score", "snake!case"]:
        r = client.post(
            "/dashboard/settings/status-page-slug",
            data={"slug": bad},
            follow_redirects=False,
        )
        # Validation errors redirect back to settings with ?error=...
        assert r.status_code == 303
        assert "error" in r.headers["location"], f"slug {bad!r} should have errored"


def test_slug_form_rejects_reserved_words(client):
    signup_and_verify(client, "reserved@example.com", name="Res Co")
    r = client.post(
        "/dashboard/settings/status-page-slug",
        data={"slug": "admin"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error" in r.headers["location"]


def test_slug_form_rejects_taken_slug(app_ctx):
    """Two users can't share a slug: the second must be told it's taken."""
    from starlette.testclient import TestClient

    from tests.conftest import install_csrf_autoinject

    with TestClient(app_ctx.app) as c1:
        install_csrf_autoinject(c1)
        signup_and_verify(c1, "a@example.com", name="A Co")
        c1.post(
            "/dashboard/settings/status-page-slug",
            data={"slug": "shared-slug"},
            follow_redirects=False,
        )

    with TestClient(app_ctx.app) as c2:
        install_csrf_autoinject(c2)
        signup_and_verify(c2, "b@example.com", name="B Co")
        r = c2.post(
            "/dashboard/settings/status-page-slug",
            data={"slug": "shared-slug"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "error" in r.headers["location"]
