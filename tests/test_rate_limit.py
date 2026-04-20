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
