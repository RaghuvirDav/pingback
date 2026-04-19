from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pingback.db.connection import get_database
from pingback.encryption import decrypt_value

_bearer_scheme = HTTPBearer()
_bearer_scheme_optional = HTTPBearer(auto_error=False)


def hash_api_key(token: str) -> str:
    """Produce a deterministic SHA-256 hash of an API key for DB lookup."""
    return hashlib.sha256(token.encode()).hexdigest()


async def _lookup_user(token: str) -> dict | None:
    """Look up a user by API key. Tries api_key_hash first, falls back to plaintext column."""
    db = await get_database()
    token_hash = hash_api_key(token)
    # Primary lookup via hash column (encrypted-era keys)
    async with db.execute(
        "SELECT id, email, name, plan, stripe_customer_id, stripe_subscription_id FROM users WHERE api_key_hash = ?",
        (token_hash,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        # Fallback: legacy plaintext api_key lookup (pre-encryption data)
        async with db.execute(
            "SELECT id, email, name, plan, stripe_customer_id, stripe_subscription_id FROM users WHERE api_key = ?",
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
        "stripe_customer_id": row["stripe_customer_id"],
        "stripe_subscription_id": row["stripe_subscription_id"],
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
