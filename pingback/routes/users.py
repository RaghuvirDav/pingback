from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from pingback.auth import get_current_user, hash_api_key
from pingback.db.connection import get_database
from pingback.encryption import decrypt_value, encrypt_value
from pingback.rate_limit import require_rate_limit

router = APIRouter(prefix="/api")


@router.delete("/users/{user_id}", status_code=204, dependencies=[Depends(require_rate_limit)])
async def delete_user(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """GDPR right to erasure: cascade-delete user and all associated data."""
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    db = await get_database()
    # Foreign keys with ON DELETE CASCADE handle monitors and check_results
    cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/users/{user_id}/export")
async def export_user_data(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """GDPR right to data portability: export all user data as JSON."""
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    db = await get_database()

    # Fetch user profile
    async with db.execute(
        "SELECT id, email, name, plan, created_at, updated_at, consent_given_at FROM users WHERE id = ?",
        (user_id,),
    ) as cursor:
        user_row = await cursor.fetchone()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = {
        "id": user_row["id"],
        "email": decrypt_value(user_row["email"]),
        "name": user_row["name"],
        "plan": user_row["plan"],
        "created_at": user_row["created_at"],
        "updated_at": user_row["updated_at"],
        "consent_given_at": user_row["consent_given_at"],
    }

    # Fetch monitors
    async with db.execute(
        "SELECT id, name, url, interval_seconds, status, is_public, created_at, updated_at FROM monitors WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ) as cursor:
        monitor_rows = await cursor.fetchall()

    monitors = []
    for m in monitor_rows:
        # Fetch check results for each monitor
        async with db.execute(
            "SELECT id, status, status_code, response_time_ms, error, checked_at FROM check_results WHERE monitor_id = ? ORDER BY checked_at",
            (m["id"],),
        ) as cursor:
            check_rows = await cursor.fetchall()

        monitors.append({
            "id": m["id"],
            "name": m["name"],
            "url": m["url"],
            "interval_seconds": m["interval_seconds"],
            "status": m["status"],
            "is_public": bool(m["is_public"]),
            "created_at": m["created_at"],
            "updated_at": m["updated_at"],
            "check_results": [
                {
                    "id": c["id"],
                    "status": c["status"],
                    "status_code": c["status_code"],
                    "response_time_ms": c["response_time_ms"],
                    "error": c["error"],
                    "checked_at": c["checked_at"],
                }
                for c in check_rows
            ],
        })

    return {
        "user": user_data,
        "monitors": monitors,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/users/{user_id}/consent", status_code=200)
async def record_consent(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Record GDPR consent timestamp for the user."""
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        "UPDATE users SET consent_given_at = ?, updated_at = ? WHERE id = ?",
        (now, now, user_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"consent_given_at": now}


@router.post("/users/{user_id}/rotate-key", status_code=200, dependencies=[Depends(require_rate_limit)])
async def rotate_api_key(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Rotate the user's API key. The old key is invalidated immediately."""
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    db = await get_database()
    new_key = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        "UPDATE users SET api_key = ?, api_key_hash = ?, updated_at = ? WHERE id = ?",
        (encrypt_value(new_key), hash_api_key(new_key), now, user_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"api_key": new_key}
