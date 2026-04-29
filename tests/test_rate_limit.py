"""Rate-limit tests — white-box: the auth limiter kicks in above threshold."""
from __future__ import annotations


def test_rate_limiter_blocks_over_threshold(app_ctx):
    from pingback.rate_limit import RateLimiter
    from fastapi import HTTPException

    rl = RateLimiter(max_requests=3, window_seconds=60)

    class FakeRequest:
        class _client:
            host = "203.0.113.42"
        client = _client()

    req = FakeRequest()
    rl.check(req)
    rl.check(req)
    rl.check(req)
    try:
        rl.check(req)
        raise AssertionError("expected rate limit to trigger")
    except HTTPException as exc:
        assert exc.status_code == 429


def test_rate_limiter_separates_ips(app_ctx):
    from pingback.rate_limit import RateLimiter

    rl = RateLimiter(max_requests=1, window_seconds=60)

    class Req:
        def __init__(self, ip):
            self.client = type("c", (), {"host": ip})

    rl.check(Req("10.0.0.1"))
    rl.check(Req("10.0.0.2"))  # Different IP is fine


# ---------------------------------------------------------------------------
# MAK-166: per-route limits on the pre-auth POST endpoints. Black-box: we hit
# the actual routes through TestClient and assert the (N+1)th attempt 429s.
# ---------------------------------------------------------------------------


def test_login_returns_429_after_5_attempts_in_a_minute(client):
    """Issue MAK-166: 'unit-test that the 6th login in a minute returns 429'."""
    payload = {"email": "noone@example.com", "password": "wrong-password"}
    for _ in range(5):
        r = client.post("/login", data=payload, follow_redirects=False)
        # Each attempt before the cap should hit the route logic — generic
        # 401 for an unknown account, never 429 yet.
        assert r.status_code == 401, r.text[:200]
    r = client.post("/login", data=payload, follow_redirects=False)
    assert r.status_code == 429, r.text[:200]


def test_signup_returns_429_after_3_attempts_in_a_minute(client):
    from tests.conftest import TEST_PASSWORD

    for i in range(3):
        r = client.post(
            "/signup",
            data={"email": f"rl-{i}@example.com", "password": TEST_PASSWORD},
            follow_redirects=False,
        )
        # Successful signup renders the "check your email" page (200).
        assert r.status_code == 200, r.text[:200]
    r = client.post(
        "/signup",
        data={"email": "rl-blocked@example.com", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 429, r.text[:200]


def test_forgot_password_returns_429_after_3_attempts_per_email(client):
    """Per-email bucket — 3 resets/hour to one address even from rotating IPs."""
    target = "victim@example.com"
    for _ in range(3):
        r = client.post(
            "/forgot-password", data={"email": target}, follow_redirects=False,
        )
        assert r.status_code == 200, r.text[:200]
    r = client.post(
        "/forgot-password", data={"email": target}, follow_redirects=False,
    )
    assert r.status_code == 429, r.text[:200]


def test_forgot_password_email_bucket_is_case_insensitive(client):
    """`Victim@Example.COM` and `victim@example.com` must share a bucket — the
    point of the email key is to stop spam to one address, regardless of how
    the attacker capitalised it."""
    for email in (
        "Spam@Example.com",
        "SPAM@example.com",
        "spam@example.com  ",
    ):
        r = client.post(
            "/forgot-password", data={"email": email}, follow_redirects=False,
        )
        assert r.status_code == 200, r.text[:200]
    r = client.post(
        "/forgot-password", data={"email": "spam@example.com"}, follow_redirects=False,
    )
    assert r.status_code == 429, r.text[:200]


def test_reset_password_returns_429_after_token_bucket_exhausted(client):
    """Even with a bogus token, repeated /reset-password POSTs must 429 once
    the per-token bucket is exhausted — keeps scripted token guessing cheap
    for us and expensive for the attacker."""
    fake_token = "a" * 43  # shaped like generate_token() output
    payload = {"token": fake_token, "password": "new-password-xyz"}
    for _ in range(5):
        r = client.post("/reset-password", data=payload, follow_redirects=False)
        # Bogus token → 400 from the route.
        assert r.status_code == 400, r.text[:200]
    r = client.post("/reset-password", data=payload, follow_redirects=False)
    assert r.status_code == 429, r.text[:200]
