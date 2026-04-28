"""Internal admin monitoring dashboard for the board (MAK-142).

Single server-rendered page at /admin showing operational state across all
users: monitor inventory, user/plan counts, and a recent-failures feed.

Access is gated by an ADMIN_EMAILS allowlist (env var, comma-separated).
A non-allowlisted caller — including a logged-out visitor — gets a 404 so
the route is invisible to anyone who shouldn't know it exists. The board's
`_ADMIN_PLANS = {"business"}` audit gate is deliberately NOT reused: a
business customer is not Pingback ops and must not see other users' data.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from pingback.config import ADMIN_EMAILS, SENTRY_DASHBOARD_URL
from pingback.db.connection import get_database
from pingback.encryption import decrypt_value
from pingback.routes.dashboard import _get_ui_user

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

logger = logging.getLogger("pingback.admin")
router = APIRouter()

# Cap on the live monitor list. The board's view doesn't need to scroll past
# this; the count card still reports the true total.
_MONITOR_LIMIT = 200
# Cap on the failures feed — a one-glance "what's broken right now".
_ERROR_LIMIT = 50


async def _require_admin(request: Request) -> dict:
    if not ADMIN_EMAILS:
        raise HTTPException(status_code=404)
    user = await _get_ui_user(request)
    if user is None:
        raise HTTPException(status_code=404)
    email = decrypt_value(user["email"]).strip().lower()
    if email not in ADMIN_EMAILS:
        raise HTTPException(status_code=404)
    return user


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    await _require_admin(request)
    db = await get_database()

    async with db.execute(
        "SELECT plan, COUNT(*) AS n FROM users GROUP BY plan"
    ) as cursor:
        plan_rows = await cursor.fetchall()
    plan_counts = {row["plan"]: row["n"] for row in plan_rows}
    total_users = sum(plan_counts.values())
    paid_users = plan_counts.get("pro", 0) + plan_counts.get("business", 0)

    async with db.execute(
        "SELECT COUNT(*) AS n FROM monitors WHERE status = 'active'"
    ) as cursor:
        row = await cursor.fetchone()
    active_monitor_count = row["n"] if row else 0

    async with db.execute(
        """
        SELECT m.id, m.name, m.url, m.status, m.interval_seconds, m.user_id,
               m.updated_at, u.email AS owner_email_enc
        FROM monitors m
        JOIN users u ON u.id = m.user_id
        WHERE m.status = 'active'
        ORDER BY m.updated_at DESC
        LIMIT ?
        """,
        (_MONITOR_LIMIT,),
    ) as cursor:
        monitor_rows = await cursor.fetchall()

    monitors = [
        {
            "id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "status": row["status"],
            "interval_seconds": row["interval_seconds"],
            "owner_email": decrypt_value(row["owner_email_enc"]),
            "updated_at": row["updated_at"],
        }
        for row in monitor_rows
    ]

    async with db.execute(
        """
        SELECT cr.checked_at, cr.status, cr.status_code, cr.error,
               m.name AS monitor_name, m.url AS monitor_url,
               u.email AS owner_email_enc
        FROM check_results cr
        JOIN monitors m ON m.id = cr.monitor_id
        JOIN users u ON u.id = m.user_id
        WHERE cr.status IN ('down', 'error')
        ORDER BY cr.checked_at DESC
        LIMIT ?
        """,
        (_ERROR_LIMIT,),
    ) as cursor:
        error_rows = await cursor.fetchall()

    errors = [
        {
            "checked_at": row["checked_at"],
            "status": row["status"],
            "status_code": row["status_code"],
            "error": row["error"],
            "monitor_name": row["monitor_name"],
            "monitor_url": row["monitor_url"],
            "owner_email": decrypt_value(row["owner_email_enc"]),
        }
        for row in error_rows
    ]

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "total_users": total_users,
            "paid_users": paid_users,
            "plan_counts": {
                "free": plan_counts.get("free", 0),
                "pro": plan_counts.get("pro", 0),
                "business": plan_counts.get("business", 0),
            },
            "active_monitor_count": active_monitor_count,
            "monitor_list_capped": active_monitor_count > _MONITOR_LIMIT,
            "monitor_limit": _MONITOR_LIMIT,
            "monitors": monitors,
            "errors": errors,
            "error_limit": _ERROR_LIMIT,
            "sentry_dashboard_url": SENTRY_DASHBOARD_URL,
        },
    )
