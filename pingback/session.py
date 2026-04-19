"""Cookie-based session auth for the dashboard UI.

Stores the user's API key in a signed cookie so the browser can authenticate
against the same user-lookup used by the JSON API.
"""
from __future__ import annotations

import hashlib
import hmac
import base64

from fastapi import Request, Response
from pingback.config import ENCRYPTION_KEY

COOKIE_NAME = "pb_session"
# Use ENCRYPTION_KEY as HMAC secret; fall back to a dev-only default
_SECRET = (ENCRYPTION_KEY or "pingback-dev-secret-change-me").encode()


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
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 days
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def get_session_key(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return _verify(token)
