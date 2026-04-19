from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pingback.db.connection import get_database

_bearer_scheme = HTTPBearer()
_bearer_scheme_optional = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Validate a Bearer token against the users.api_key column.

    Returns a dict with at least ``id`` and ``email`` for the authenticated user.
    Raises 401 if the token is missing or invalid.
    """
    token = credentials.credentials
    db = await get_database()
    async with db.execute(
        "SELECT id, email, name, plan FROM users WHERE api_key = ?",
        (token,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = {"id": row["id"], "email": row["email"], "name": row["name"], "plan": row["plan"]}
    request.state.audit_user_id = user["id"]
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme_optional),
) -> dict | None:
    """Return the authenticated user if a valid Bearer token is provided, else None."""
    if credentials is None:
        return None
    token = credentials.credentials
    db = await get_database()
    async with db.execute(
        "SELECT id, email, name, plan FROM users WHERE api_key = ?",
        (token,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"], "plan": row["plan"]}
