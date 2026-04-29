"""Public status page — server-rendered with the shared design system."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from pingback.csrf import register_csrf_globals
from pingback.db.connection import get_database
from pingback.db.monitors import (
    find_monitors_with_last_check,
    get_30day_uptime,
    get_response_times,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_csrf_globals(templates)

router = APIRouter()


def _overall_status(monitors: list[dict]) -> tuple[str, str]:
    """Return (css_class, label) for the overall status banner."""
    if not monitors:
        return "operational", "All systems operational"
    statuses = [m["current_status"] for m in monitors]
    down_count = statuses.count("down") + statuses.count("error")
    if down_count == 0:
        return "operational", "All systems operational"
    if down_count == len(statuses):
        return "major", "Major outage"
    return "partial", "Partial outage"


@router.get("/status/{user_id}", response_class=HTMLResponse)
async def public_status_page(request: Request, user_id: str):
    db = await get_database()
    monitors_with_checks = await find_monitors_with_last_check(db, user_id)

    if not monitors_with_checks:
        async with db.execute("SELECT id FROM users WHERE id = ?", (user_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="User not found")

    view_monitors = []
    for mwc in monitors_with_checks:
        if not getattr(mwc, "is_public", False):
            continue

        uptime = await get_30day_uptime(db, mwc.id)
        rts_raw = await get_response_times(db, mwc.id, limit=50)

        current_status = "unknown"
        last_response_ms = None
        last_checked = None
        if mwc.last_check:
            current_status = mwc.last_check.status
            last_response_ms = mwc.last_check.response_time_ms
            last_checked = mwc.last_check.checked_at

        response_times = []
        if rts_raw:
            max_ms = max(r["response_time_ms"] for r in rts_raw) or 1
            response_times = [
                {
                    "ms": r["response_time_ms"],
                    "height": max(5, int(r["response_time_ms"] / max_ms * 100)),
                }
                for r in rts_raw
            ]

        view_monitors.append({
            "name": mwc.name,
            "current_status": current_status,
            "uptime": uptime,
            "last_response_ms": last_response_ms,
            "last_checked": last_checked,
            "response_times": response_times,
        })

    overall_class, overall_label = _overall_status(view_monitors)

    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "user_id": user_id,
            "monitors": view_monitors,
            "overall_class": overall_class,
            "overall_label": overall_label,
        },
    )
