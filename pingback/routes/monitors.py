from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from pingback.auth import get_current_user, get_optional_user
from pingback.db.connection import get_database
from pingback.db.monitors import (
    create_monitor,
    delete_monitor,
    find_monitor_by_id,
    find_monitors_with_last_check,
)
from pingback.models import CreateMonitorInput, Monitor, MonitorWithLastCheck, PublicMonitor

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
        is_public=body.is_public,
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


@router.get("/monitors/{monitor_id}", response_model=PublicMonitor)
async def get_monitor(
    monitor_id: str,
    current_user: dict | None = Depends(get_optional_user),
):
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    # Authenticated owner can always see their own monitor
    is_owner = current_user is not None and current_user["id"] == monitor.user_id
    if not monitor.is_public and not is_owner:
        raise HTTPException(status_code=404, detail="Monitor not found")
    # Owner gets full Monitor; everyone else gets PublicMonitor (no user_id)
    if is_owner:
        return monitor
    return PublicMonitor(**{k: v for k, v in monitor.model_dump().items() if k != "user_id"})


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
