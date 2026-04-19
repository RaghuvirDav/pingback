from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pingback.db.connection import get_database
from pingback.db.monitors import find_monitor_by_id, get_check_history, get_last_check
from pingback.models import CheckResult

router = APIRouter(prefix="/api")


@router.get("/monitors/{monitor_id}/checks", response_model=list[CheckResult])
async def list_checks(monitor_id: str, limit: int = Query(default=100, ge=1, le=1000)):
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return await get_check_history(db, monitor_id, limit)


@router.get("/monitors/{monitor_id}/checks/latest", response_model=CheckResult)
async def latest_check(monitor_id: str):
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    check = await get_last_check(db, monitor_id)
    if check is None:
        raise HTTPException(status_code=404, detail="No checks yet")
    return check
