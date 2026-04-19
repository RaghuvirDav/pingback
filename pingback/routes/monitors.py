from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from pingback.auth import get_current_user
from pingback.db.connection import get_database
from pingback.db.monitors import (
    create_monitor,
    delete_monitor,
    find_monitor_by_id,
    find_monitors_with_last_check,
)
from pingback.models import CreateMonitorInput, Monitor, MonitorWithLastCheck

router = APIRouter(prefix="/api")


@router.post("/monitors", status_code=201, response_model=Monitor)
async def create_monitor_route(
    body: CreateMonitorInput,
    current_user: dict = Depends(get_current_user),
):
    db = await get_database()
    monitor = await create_monitor(
        db,
        user_id=current_user["id"],
        name=body.name,
        url=str(body.url),
        interval_seconds=body.interval_seconds,
    )
    return monitor


@router.get("/users/{user_id}/monitors", response_model=list[MonitorWithLastCheck])
async def list_user_monitors(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    if current_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    db = await get_database()
    return await find_monitors_with_last_check(db, user_id)


@router.get("/monitors/{monitor_id}", response_model=Monitor)
async def get_monitor(monitor_id: str):
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor


@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor_route(
    monitor_id: str,
    current_user: dict = Depends(get_current_user),
):
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if monitor.user_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    await delete_monitor(db, monitor_id)
