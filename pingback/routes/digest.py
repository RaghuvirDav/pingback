from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from pingback.auth import get_current_user
from pingback.db.connection import get_database
from pingback.db.digest import disable_digest_by_token, get_digest_pref, upsert_digest_pref

router = APIRouter(prefix="/api/digest")


class DigestPrefInput(BaseModel):
    enabled: bool = True
    send_hour_utc: int = 8


@router.get("/preferences")
async def get_preferences(current_user: dict = Depends(get_current_user)):
    """Get the current user's digest email preferences."""
    db = await get_database()
    pref = await get_digest_pref(db, current_user["id"])
    if pref is None:
        return {"enabled": False, "send_hour_utc": 8, "subscribed": False}
    return {
        "enabled": bool(pref["enabled"]),
        "send_hour_utc": pref["send_hour_utc"],
        "last_sent_at": pref["last_sent_at"],
        "subscribed": True,
    }


@router.put("/preferences")
async def update_preferences(
    body: DigestPrefInput,
    current_user: dict = Depends(get_current_user),
):
    """Subscribe to or update daily digest email preferences."""
    if not 0 <= body.send_hour_utc <= 23:
        raise HTTPException(status_code=400, detail="send_hour_utc must be 0–23")

    db = await get_database()

    # Check GDPR consent before enabling
    if body.enabled:
        async with db.execute(
            "SELECT consent_given_at FROM users WHERE id = ?",
            (current_user["id"],),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["consent_given_at"] is None:
            raise HTTPException(
                status_code=400,
                detail="GDPR consent required before enabling digest emails. POST /api/users/{user_id}/consent first.",
            )

    pref = await upsert_digest_pref(db, current_user["id"], body.enabled, body.send_hour_utc)
    return {
        "enabled": bool(pref["enabled"]),
        "send_hour_utc": pref["send_hour_utc"],
        "last_sent_at": pref["last_sent_at"],
    }


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(token: str = Query(...)):
    """One-click unsubscribe via token (no auth required — link from email)."""
    db = await get_database()
    ok = await disable_digest_by_token(db, token)
    if not ok:
        return HTMLResponse(
            "<html><body><h2>Invalid or expired unsubscribe link.</h2></body></html>",
            status_code=400,
        )
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:48px;'>"
        "<h2>You've been unsubscribed</h2>"
        "<p>You will no longer receive daily digest emails from Pingback.</p>"
        "<p>You can re-enable digests anytime from your dashboard.</p>"
        "</body></html>"
    )
