from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from pingback.auth import get_current_user
from pingback.db.connection import get_database
from pingback.rate_limit import require_rate_limit

router = APIRouter(prefix="/api")

# Plans that have admin access to audit logs
_ADMIN_PLANS = {"business"}


@router.get("/audit-log", dependencies=[Depends(require_rate_limit)])
async def get_audit_log(
    current_user: dict = Depends(get_current_user),
    action: Optional[str] = Query(None, description="Filter by action (read, create, update, delete)"),
    resource_type: Optional[str] = Query(None, description="Filter by resource type"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Admin-only endpoint to query the HIPAA audit trail."""
    if current_user["plan"] not in _ADMIN_PLANS:
        raise HTTPException(status_code=403, detail="Audit log access requires a business plan")

    db = await get_database()
    conditions = []
    params: list = []

    if action:
        conditions.append("action = ?")
        params.append(action)
    if resource_type:
        conditions.append("resource_type = ?")
        params.append(resource_type)
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    query = f"SELECT id, user_id, action, resource_type, resource_id, ip_address, detail, timestamp FROM audit_log {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "action": row["action"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "ip_address": row["ip_address"],
            "detail": row["detail"],
            "timestamp": row["timestamp"],
        }
        for row in rows
    ]
