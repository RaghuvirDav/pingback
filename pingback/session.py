"""Cookie-based session auth for the dashboard UI.

MAK-167 rewrite: the cookie used to carry the user's signed API key in
plaintext, so any cookie compromise was an API-key compromise too. The cookie
now carries only an opaque session id (256-bit random token, signed with
``SESSION_SECRET`` for forgery resistance); the mapping ``session_id ->
user_id`` lives server-side in the ``sessions`` table. The API key never
touches the browser after the one-time reveal at signup.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import Request, Response

from pingback.config import SESSION_LIFETIME_SECONDS, SESSION_SECRET

COOKIE_NAME = "pb_session"

# Dev-only fallback so local non-production runs don't have to set
# SESSION_SECRET. `validate_secrets()` in pingback.config refuses to boot in
# production with this value, so it can never reach a real deployment.
_DEV_FALLBACK_SECRET = "pingback-dev-secret-change-me"
_SECRET = (SESSION_SECRET or _DEV_FALLBACK_SECRET).encode()


def _cookie_secure() -> bool:
    """Return True when cookies should be flagged ``Secure`` (HTTPS-only).

    Controlled by the ``SESSION_COOKIE_SECURE`` env var. Default off for local
    dev so HTTP localhost still works; set to ``1`` in any production-like
    deployment (or whenever ``APP_ENV=production``, as a safety net)."""
    flag = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("APP_ENV", "").strip().lower() == "production":
        return True
    return False


def _sign_session_id(session_id: str) -> str:
    """Return a cookie value of the form ``<session_id>.<hmac>``.

    HMAC isn't strictly required for opaque 256-bit ids — guessing one is
    infeasible — but it lets us reject obviously-forged cookies before they
    hit the DB, which protects the sessions table from random scanners."""
    sig = hmac.new(_SECRET, session_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{session_id}.{sig}"


def _verify_signed_session_id(token: str) -> str | None:
    """Inverse of ``_sign_session_id``. Returns the raw session id, or None."""
    if not token or "." not in token:
        return None
    session_id, sig = token.rsplit(".", 1)
    expected = hmac.new(_SECRET, session_id.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        return None
    return session_id


def _new_session_id() -> str:
    """Random 256-bit opaque session id, URL-safe."""
    return secrets.token_urlsafe(32)


async def create_session(db: aiosqlite.Connection, user_id: str) -> str:
    """Insert a fresh session row for ``user_id`` and return the new id."""
    session_id = _new_session_id()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=SESSION_LIFETIME_SECONDS)
    await db.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, now.isoformat(), expires_at.isoformat()),
    )
    await db.commit()
    return session_id


async def delete_session(db: aiosqlite.Connection, session_id: str) -> None:
    """Hard-delete a session row by id (logout / rotate)."""
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    await db.commit()


async def delete_sessions_for_user(db: aiosqlite.Connection, user_id: str) -> None:
    """Hard-delete every session row for a user (force-logout-everywhere)."""
    await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    await db.commit()


async def lookup_session_user_id(
    db: aiosqlite.Connection, session_id: str
) -> str | None:
    """Return the ``user_id`` for ``session_id`` if the row exists and has not
    expired; otherwise return None and best-effort delete the stale row."""
    async with db.execute(
        "SELECT user_id, expires_at FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    user_id = row["user_id"]
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        try:
            await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await db.commit()
        except Exception:
            pass  # best effort — eviction will catch it later
        return None
    return user_id


def set_session_cookie(response: Response, session_id: str) -> None:
    """Write the signed-session-id cookie onto ``response``."""
    response.set_cookie(
        COOKIE_NAME,
        _sign_session_id(session_id),
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=SESSION_LIFETIME_SECONDS,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def read_session_cookie(request: Request) -> str | None:
    """Extract the raw (unsigned) session id from the request cookie, or None
    if the cookie is missing / signature doesn't match."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return _verify_signed_session_id(token)
