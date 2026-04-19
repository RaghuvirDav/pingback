from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pingback.db.connection import get_database

_bearer_scheme = HTTPBearer()


async def get_current_user(
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
    return {"id": row["id"], "email": row["email"], "name": row["name"], "plan": row["plan"]}
