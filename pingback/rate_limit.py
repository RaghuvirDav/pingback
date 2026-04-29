from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Form, HTTPException, Request, status


class RateLimiter:
    """Simple in-memory sliding-window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    @staticmethod
    def _ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _record(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._hits[key] = [t for t in self._hits[key] if t > cutoff]
        if len(self._hits[key]) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )
        self._hits[key].append(now)

    def check(self, request: Request) -> None:
        """IP-keyed check (the default)."""
        self._record(self._ip(request))

    def check_key(self, key: str) -> None:
        """Arbitrary-key check — used when the bucket is per-email or per-token
        rather than per-IP."""
        self._record(key)


# Shared limiter for authenticated JSON endpoints (audit, users, monitors).
# Kept at the historical 20/min/IP — these routes already require a valid API
# key, so spray attacks aren't the threat here. MAK-166 only tightens the
# pre-auth POST routes (login/signup/forgot/reset) below.
auth_rate_limiter = RateLimiter(max_requests=20, window_seconds=60)


def require_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces the shared auth rate limit."""
    auth_rate_limiter.check(request)


# MAK-166: per-route limiters for the pre-auth POST endpoints. These guard
# against credential-stuffing/spray. The IP-only buckets catch a single host
# hammering many accounts; the email-keyed bucket on /forgot-password catches
# distributed spray against one address (and stops mass reset-email spam).
login_rate_limiter = RateLimiter(max_requests=5, window_seconds=60)
signup_rate_limiter = RateLimiter(max_requests=3, window_seconds=60)
# /forgot-password: keep an IP cap loose enough to survive shared NATs while
# the per-email bucket caps spam to any one address at 3/hr.
forgot_ip_rate_limiter = RateLimiter(max_requests=20, window_seconds=3600)
forgot_email_rate_limiter = RateLimiter(max_requests=3, window_seconds=3600)
# /reset-password: brute-forcing the token itself is infeasible (256 random
# bits), but a token-keyed cap is cheap insurance against scripted probes.
reset_ip_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
reset_token_rate_limiter = RateLimiter(max_requests=5, window_seconds=60)


def require_login_rate_limit(request: Request) -> None:
    login_rate_limiter.check(request)


def require_signup_rate_limit(request: Request) -> None:
    signup_rate_limiter.check(request)


def require_forgot_rate_limit(
    request: Request,
    email: str = Form(...),
) -> None:
    forgot_ip_rate_limiter.check(request)
    forgot_email_rate_limiter.check_key(f"forgot-email|{email.strip().lower()}")


def require_reset_rate_limit(
    request: Request,
    token: str = Form(...),
) -> None:
    reset_ip_rate_limiter.check(request)
    reset_token_rate_limiter.check_key(f"reset-token|{token}")
