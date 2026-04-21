"""Cookie-based session auth for the dashboard UI.

Stores the user's API key in a signed cookie so the browser can authenticate
against the same user-lookup used by the JSON API.
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import os

from fastapi import Request, Response
from pingback.config import ENCRYPTION_KEY

COOKIE_NAME = "pb_session"
SIGNUP_REVEAL_COOKIE = "pb_signup_reveal"
# 10-minute window after signup during which the plaintext API key may be
# re-rendered on /signup/success. Intentionally short so the key stops being
# reachable shortly after the user leaves the tab.
SIGNUP_REVEAL_TTL_SECONDS = 600
# Use ENCRYPTION_KEY as HMAC secret; fall back to a dev-only default
_SECRET = (ENCRYPTION_KEY or "pingback-dev-secret-change-me").encode()


def _cookie_secure() -> bool:
    """Return True when cookies should be flagged `Secure` (HTTPS-only).

    Controlled by the `SESSION_COOKIE_SECURE` env var. Default off for local
    dev so HTTP localhost still works; set to `1` in any production-like
    deployment (or whenever `APP_ENV=production`, as a safety net)."""
    flag = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("APP_ENV", "").strip().lower() == "production":
        return True
    return False


def _sign(value: str) -> str:
    sig = hmac.new(_SECRET, value.encode(), hashlib.sha256).hexdigest()[:16]
    encoded = base64.urlsafe_b64encode(value.encode()).decode()
    return f"{encoded}.{sig}"


def _verify(token: str) -> str | None:
    if "." not in token:
        return None
    encoded, sig = token.rsplit(".", 1)
    try:
        value = base64.urlsafe_b64decode(encoded).decode()
    except Exception:
        return None
    expected = hmac.new(_SECRET, value.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        return None
    return value


def set_session(response: Response, api_key: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _sign(api_key),
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 days
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def set_signup_reveal(response: Response) -> None:
    response.set_cookie(
        SIGNUP_REVEAL_COOKIE,
        "1",
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=SIGNUP_REVEAL_TTL_SECONDS,
        path="/",
    )


def has_signup_reveal(request: Request) -> bool:
    return request.cookies.get(SIGNUP_REVEAL_COOKIE) == "1"


def clear_signup_reveal(response: Response) -> None:
    response.delete_cookie(SIGNUP_REVEAL_COOKIE, path="/")


def get_session_key(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return _verify(token)
