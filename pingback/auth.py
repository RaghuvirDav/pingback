from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pingback.db.connection import get_database
from pingback.encryption import decrypt_value

_bearer_scheme = HTTPBearer()
_bearer_scheme_optional = HTTPBearer(auto_error=False)

VERIFICATION_TTL_HOURS = 24
RESET_TTL_HOURS = 2
MIN_PASSWORD_LENGTH = 8
# bcrypt 5.x refuses inputs longer than 72 bytes. We pre-hash with SHA-256
# and truncate at that limit so arbitrary-length passwords still round-trip,
# while keeping the stored hash algorithm unambiguously bcrypt.
_BCRYPT_MAX_BYTES = 72


def _bcrypt_input(password: str) -> bytes:
    data = password.encode("utf-8")
    if len(data) <= _BCRYPT_MAX_BYTES:
        return data
    # SHA-256 hash fits in 64 bytes (hex-encoded) — well under the 72-byte cap.
    return hashlib.sha256(data).hexdigest().encode("ascii")


def hash_api_key(token: str) -> str:
    """Produce a deterministic SHA-256 hash of an API key for DB lookup."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_email(email: str) -> str:
    """Deterministic hash of a user email (case-insensitive), for dedup lookup.

    Fernet encryption is non-deterministic so we can't UNIQUE-index the
    encrypted `email` column. Store a SHA-256 hash of the normalised email in
    `email_hash` and enforce uniqueness there.
    """
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt (12 rounds)."""
    return bcrypt.hashpw(_bcrypt_input(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a plaintext password against a stored bcrypt hash.

    Returns False if `password_hash` is None/empty, so callers can treat
    "no password on file" and "wrong password" identically at the API surface
    (don't leak which accounts exist).
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_bcrypt_input(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_token() -> str:
    """Generate a URL-safe single-use token for email verification / reset."""
    return secrets.token_urlsafe(32)


def token_expiry(hours: int) -> str:
    """ISO-8601 UTC timestamp `hours` in the future."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def is_token_expired(expires_at: str | None) -> bool:
    """Return True if an ISO-8601 expiry timestamp is in the past or missing."""
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < datetime.now(timezone.utc)


async def _lookup_user(token: str) -> dict | None:
    """Look up a user by API key. Tries api_key_hash first, falls back to plaintext column."""
    db = await get_database()
    token_hash = hash_api_key(token)
    # Primary lookup via hash column (encrypted-era keys)
    async with db.execute(
        """SELECT id, email, name, plan, paddle_customer_id, paddle_subscription_id,
                  paddle_subscription_status, plan_renews_at, plan_cancel_at,
                  email_verified, timezone, status_page_slug
             FROM users WHERE api_key_hash = ?""",
        (token_hash,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        # Fallback: legacy plaintext api_key lookup (pre-encryption data)
        async with db.execute(
            """SELECT id, email, name, plan, paddle_customer_id, paddle_subscription_id,
                      paddle_subscription_status, plan_renews_at, plan_cancel_at,
                      email_verified, timezone, status_page_slug
                 FROM users WHERE api_key = ?""",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    # Bump last_login_at so abandoned-account cleanup knows the user is active
    await db.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), row["id"]),
    )
    await db.commit()
    return {
        "id": row["id"],
        "email": decrypt_value(row["email"]),
        "name": row["name"],
        "plan": row["plan"],
        "paddle_customer_id": row["paddle_customer_id"],
        "paddle_subscription_id": row["paddle_subscription_id"],
        "paddle_subscription_status": row["paddle_subscription_status"],
        "plan_renews_at": row["plan_renews_at"],
        "plan_cancel_at": row["plan_cancel_at"],
        "email_verified": bool(row["email_verified"]),
        "timezone": row["timezone"] or "Etc/UTC",
        "status_page_slug": row["status_page_slug"],
    }


async def lookup_user_by_id(user_id: str) -> dict | None:
    """Look up a user by primary key. Mirrors the projection used by the
    cookie-session UI lookup so dashboard `_get_ui_user(...)` keeps working
    after MAK-167 stopped putting the API key in the cookie."""
    db = await get_database()
    async with db.execute(
        """SELECT id, email, name, plan, paddle_customer_id, paddle_subscription_id,
                  paddle_subscription_status, plan_renews_at, plan_cancel_at,
                  email_verified, timezone
             FROM users WHERE id = ?""",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    await db.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), row["id"]),
    )
    await db.commit()
    return {
        "id": row["id"],
        "email": decrypt_value(row["email"]),
        "name": row["name"],
        "plan": row["plan"],
        "paddle_customer_id": row["paddle_customer_id"],
        "paddle_subscription_id": row["paddle_subscription_id"],
        "paddle_subscription_status": row["paddle_subscription_status"],
        "plan_renews_at": row["plan_renews_at"],
        "plan_cancel_at": row["plan_cancel_at"],
        "email_verified": bool(row["email_verified"]),
        "timezone": row["timezone"] or "Etc/UTC",
    }


async def lookup_user_by_email(email: str) -> dict | None:
    """Look up a user by (normalised) email. Returns full auth fields."""
    db = await get_database()
    async with db.execute(
        """SELECT id, email, name, plan, paddle_customer_id, paddle_subscription_id,
                  password_hash, email_verified, api_key, api_key_hash
           FROM users WHERE email_hash = ?""",
        (hash_email(email),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": decrypt_value(row["email"]),
        "name": row["name"],
        "plan": row["plan"],
        "paddle_customer_id": row["paddle_customer_id"],
        "paddle_subscription_id": row["paddle_subscription_id"],
        "password_hash": row["password_hash"],
        "email_verified": bool(row["email_verified"]),
        "api_key_encrypted": row["api_key"],
        "api_key_hash": row["api_key_hash"],
    }


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Validate a Bearer token against the users table.

    Returns a dict with at least ``id`` and ``email`` for the authenticated user.
    Raises 401 if the token is missing or invalid.
    """
    user = await _lookup_user(credentials.credentials)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.audit_user_id = user["id"]
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme_optional),
) -> dict | None:
    """Return the authenticated user if a valid Bearer token is provided, else None."""
    if credentials is None:
        return None
    return await _lookup_user(credentials.credentials)
