from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import HTTPException, Request, status


class RateLimiter:
    """Simple in-memory sliding-window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def _key(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def check(self, request: Request) -> None:
        key = self._key(request)
        now = time.monotonic()
        cutoff = now - self.window_seconds
        # Prune old entries
        self._hits[key] = [t for t in self._hits[key] if t > cutoff]
        if len(self._hits[key]) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )
        self._hits[key].append(now)


# Shared limiter for auth-sensitive endpoints: 20 requests per 60 seconds per IP
auth_rate_limiter = RateLimiter(max_requests=20, window_seconds=60)


def require_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces the auth rate limit."""
    auth_rate_limiter.check(request)
