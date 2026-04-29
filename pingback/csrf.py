"""Double-submit-cookie CSRF protection for state-changing form POSTs.

Defense model
-------------
- Every browser gets a long-lived ``pb_csrf`` cookie with a random opaque
  value. The cookie is httponly + SameSite=Lax (mirrors ``pb_session``).
- Form templates render a hidden ``csrf_token`` field whose value is
  ``HMAC-SHA256(ENCRYPTION_KEY, cookie_value)``. An attacker who can plant
  a cookie on our domain (e.g. via a sibling subdomain) still cannot forge
  a matching token without the server-side secret.
- POST handlers depend on :func:`csrf_protect`, which compares the form
  field (or ``X-CSRF-Token`` header) to the HMAC of the cookie. Mismatch
  or missing cookie returns 403.

This is a pure stateless validator — no Redis / DB dependency. Cookie minting
happens in the middleware so even unauthenticated visitors landing straight
on /login get a token before they submit.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request, status
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from pingback.config import APP_ENV, ENCRYPTION_KEY

CSRF_COOKIE_NAME = "pb_csrf"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

# 32 bytes of urlsafe base64 → ~43 char cookie. Plenty of entropy.
_COOKIE_VALUE_BYTES = 32
# 16 bytes (128 bits) of HMAC truncation. Plenty for a CSRF token; keeps the
# rendered form markup short.
_TOKEN_DIGEST_BYTES = 16
# Cookie lives for a year — tokens stay valid as long as the cookie does, and
# rotating it forces a benign re-render on the user's next visit.
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

_SECRET = (ENCRYPTION_KEY or "pingback-dev-secret-change-me").encode("utf-8")


def _cookie_secure() -> bool:
    """Mirror :func:`pingback.session._cookie_secure` so the two cookies have
    matching Secure flags (otherwise prod browsers reject one of them)."""
    flag = os.environ.get("CSRF_COOKIE_SECURE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    return APP_ENV == "production"


def compute_csrf_token(cookie_value: str) -> str:
    """Stable HMAC-SHA256(secret, cookie_value), hex-encoded and truncated."""
    digest = hmac.new(_SECRET, cookie_value.encode("utf-8"), hashlib.sha256).digest()
    return digest[:_TOKEN_DIGEST_BYTES].hex()


def _ensure_cookie_value(request: Request) -> str:
    """Return the live cookie value, minting one on first sight if needed.

    A freshly-minted value is stashed on ``request.state.csrf_cookie_value``
    so :class:`CSRFCookieMiddleware` can attach the Set-Cookie header to the
    outgoing response.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing:
        return existing
    pending = getattr(request.state, "csrf_cookie_value", None)
    if pending:
        return pending
    fresh = secrets.token_urlsafe(_COOKIE_VALUE_BYTES)
    request.state.csrf_cookie_value = fresh
    return fresh


def csrf_token(request: Request) -> str:
    """Jinja global: form token bound to the current browser's CSRF cookie."""
    return compute_csrf_token(_ensure_cookie_value(request))


def csrf_input(request: Request) -> Markup:
    """Jinja global: full ``<input type=hidden name=csrf_token ...>`` markup."""
    return Markup(
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" '
        f'value="{csrf_token(request)}">'
    )


def register_csrf_globals(templates) -> None:
    """Expose ``csrf_token`` / ``csrf_input`` as globals on a Jinja2Templates."""
    templates.env.globals.setdefault("csrf_token", csrf_token)
    templates.env.globals.setdefault("csrf_input", csrf_input)


class CSRFCookieMiddleware(BaseHTTPMiddleware):
    """Mint the ``pb_csrf`` cookie on first request and persist it on the response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Pre-populate state.csrf_cookie_value so the Jinja helper sees a
        # consistent value even on the first GET (before Set-Cookie lands).
        if not request.cookies.get(CSRF_COOKIE_NAME):
            request.state.csrf_cookie_value = secrets.token_urlsafe(_COOKIE_VALUE_BYTES)

        response = await call_next(request)

        pending = getattr(request.state, "csrf_cookie_value", None)
        # Only set if we actually minted one this request — harmless otherwise.
        if pending and request.cookies.get(CSRF_COOKIE_NAME) != pending:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                pending,
                max_age=_COOKIE_MAX_AGE,
                httponly=True,
                secure=_cookie_secure(),
                samesite="lax",
                path="/",
            )
        return response


async def csrf_protect(request: Request) -> None:
    """FastAPI dependency: 403 unless form ``csrf_token`` matches the cookie HMAC.

    Reading ``request.form()`` here is safe — Starlette caches the parsed
    form on the request object, so downstream ``Form(...)`` parameters do
    not re-parse the body.
    """
    cookie_val = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_val:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Missing CSRF cookie"
        )

    submitted = request.headers.get(CSRF_HEADER_NAME)
    if not submitted:
        try:
            form = await request.form()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Invalid form body"
            )
        submitted = form.get(CSRF_FIELD_NAME)

    if not submitted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Missing CSRF token"
        )
    expected = compute_csrf_token(cookie_val)
    if not hmac.compare_digest(expected, str(submitted)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )
