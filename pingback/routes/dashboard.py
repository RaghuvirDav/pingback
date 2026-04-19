"""Server-rendered dashboard UI routes (Jinja2 + Tailwind)."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pingback.auth import _lookup_user, hash_api_key
from pingback.config import APP_BASE_URL
from pingback.db.connection import get_database
from pingback.db.monitors import (
    create_monitor,
    delete_monitor,
    find_monitor_by_id,
    find_monitors_with_last_check,
    get_30day_uptime,
    get_check_history,
    get_response_times,
)
from pingback.encryption import encrypt_value
from pingback.session import clear_session, get_session_key, set_session

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_ui_user(request: Request) -> dict | None:
    """Return the authenticated user from the session cookie, or None."""
    api_key = get_session_key(request)
    if not api_key:
        return None
    return await _lookup_user(api_key)


def _require_login(user: dict | None) -> dict:
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def _redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status_code)


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    user = await _get_ui_user(request)
    return templates.TemplateResponse("landing.html", {"request": request, "user": user})


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await _get_ui_user(request)
    if user:
        return _redirect("/dashboard")
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, api_key: str = Form(...)):
    user = await _lookup_user(api_key)
    if user is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "user": None, "error": "Invalid API key. Check your key and try again."},
            status_code=401,
        )
    response = _redirect("/dashboard")
    set_session(response, api_key)
    return response


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    user = await _get_ui_user(request)
    if user:
        return _redirect("/dashboard")
    return templates.TemplateResponse(
        "signup.html", {"request": request, "user": None, "error": None, "email": "", "name": ""}
    )


@router.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request, email: str = Form(...), name: str = Form("")):
    db = await get_database()

    # Check for duplicate email
    async with db.execute("SELECT id FROM users WHERE email = ?", (email,)) as cur:
        if await cur.fetchone():
            # Also check encrypted emails
            pass
    # Try encrypted lookup too
    encrypted_email = encrypt_value(email)
    async with db.execute("SELECT id FROM users WHERE email = ?", (encrypted_email,)) as cur:
        if await cur.fetchone():
            return templates.TemplateResponse(
                "signup.html",
                {"request": request, "user": None, "error": "An account with that email already exists.", "email": email, "name": name},
                status_code=409,
            )

    user_id = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO users (id, email, name, plan, api_key, api_key_hash, created_at, updated_at, last_login_at)
           VALUES (?, ?, ?, 'free', ?, ?, ?, ?, ?)""",
        (user_id, encrypt_value(email), name or None, encrypt_value(api_key), hash_api_key(api_key), now, now, now),
    )

    # Create default digest preferences
    unsub_token = secrets.token_urlsafe(16)
    await db.execute(
        """INSERT INTO digest_preferences (user_id, enabled, send_hour_utc, unsubscribe_token, created_at, updated_at)
           VALUES (?, 1, 8, ?, ?, ?)""",
        (user_id, unsub_token, now, now),
    )
    await db.commit()

    response = _redirect("/dashboard")
    set_session(response, api_key)
    return response


@router.post("/logout")
async def logout():
    response = _redirect("/")
    clear_session(response)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    db = await get_database()
    monitors_raw = await find_monitors_with_last_check(db, user["id"])

    monitors = []
    down_count = 0
    for mwc in monitors_raw:
        uptime = await get_30day_uptime(db, mwc.id)
        current_status = "unknown"
        last_response_ms = None
        last_checked = None
        if mwc.last_check:
            current_status = mwc.last_check.status
            last_response_ms = mwc.last_check.response_time_ms
            last_checked = mwc.last_check.checked_at
        if current_status in ("down", "error"):
            down_count += 1
        monitors.append({
            "id": mwc.id,
            "name": mwc.name,
            "url": mwc.url,
            "current_status": current_status,
            "uptime": uptime,
            "last_response_ms": last_response_ms,
            "last_checked": last_checked,
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "monitors": monitors,
        "down_count": down_count,
    })


# ---------------------------------------------------------------------------
# Add / Edit monitor
# ---------------------------------------------------------------------------

@router.get("/dashboard/monitors/new", response_class=HTMLResponse)
async def new_monitor_page(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    return templates.TemplateResponse("monitor_form.html", {
        "request": request, "user": user, "monitor": None, "error": None, "name": "", "url": "",
    })


@router.post("/dashboard/monitors/new", response_class=HTMLResponse)
async def new_monitor_submit(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    interval_seconds: int = Form(300),
    is_public: int = Form(0),
):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    monitor = await create_monitor(db, user["id"], name, url, interval_seconds, bool(is_public))
    return _redirect(f"/dashboard/monitors/{monitor.id}")


@router.get("/dashboard/monitors/{monitor_id}/edit", response_class=HTMLResponse)
async def edit_monitor_page(request: Request, monitor_id: str):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None or monitor.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return templates.TemplateResponse("monitor_form.html", {
        "request": request, "user": user, "monitor": monitor, "error": None,
    })


@router.post("/dashboard/monitors/{monitor_id}/edit", response_class=HTMLResponse)
async def edit_monitor_submit(
    request: Request,
    monitor_id: str,
    name: str = Form(...),
    url: str = Form(...),
    interval_seconds: int = Form(300),
    is_public: int = Form(0),
):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None or monitor.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Monitor not found")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE monitors SET name = ?, url = ?, interval_seconds = ?, is_public = ?, updated_at = ?
           WHERE id = ?""",
        (name, url, interval_seconds, int(bool(is_public)), now, monitor_id),
    )
    await db.commit()
    return _redirect(f"/dashboard/monitors/{monitor_id}")


@router.post("/dashboard/monitors/{monitor_id}/delete")
async def delete_monitor_route(request: Request, monitor_id: str):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None or monitor.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await delete_monitor(db, monitor_id)
    return _redirect("/dashboard")


# ---------------------------------------------------------------------------
# Monitor detail
# ---------------------------------------------------------------------------

@router.get("/dashboard/monitors/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail(request: Request, monitor_id: str):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    monitor = await find_monitor_by_id(db, monitor_id)
    if monitor is None or monitor.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Monitor not found")

    uptime = await get_30day_uptime(db, monitor_id)
    checks = await get_check_history(db, monitor_id, limit=50)
    rts_raw = await get_response_times(db, monitor_id, limit=60)

    current_status = "unknown"
    last_response_ms = None
    if checks:
        current_status = checks[0].status
        last_response_ms = checks[0].response_time_ms

    response_times = []
    if rts_raw:
        max_ms = max(r["response_time_ms"] for r in rts_raw) or 1
        response_times = [
            {"ms": r["response_time_ms"], "height": max(5, int(r["response_time_ms"] / max_ms * 100)), "checked_at": r["checked_at"]}
            for r in rts_raw
        ]

    return templates.TemplateResponse("monitor_detail.html", {
        "request": request,
        "user": user,
        "monitor": monitor,
        "uptime": uptime,
        "current_status": current_status,
        "last_response_ms": last_response_ms,
        "checks": checks,
        "response_times": response_times,
    })


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/dashboard/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()

    # Load digest preferences
    digest_enabled = True
    send_hour_utc = 8
    async with db.execute(
        "SELECT enabled, send_hour_utc FROM digest_preferences WHERE user_id = ?", (user["id"],)
    ) as cur:
        row = await cur.fetchone()
        if row:
            digest_enabled = bool(row["enabled"])
            send_hour_utc = row["send_hour_utc"]

    status_url = f"{APP_BASE_URL}/status/{user['id']}"

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "digest_enabled": digest_enabled,
        "send_hour_utc": send_hour_utc,
        "status_url": status_url,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/dashboard/settings/notifications")
async def update_notifications(
    request: Request,
    send_hour_utc: int = Form(8),
    digest_enabled: int = Form(0),
):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE digest_preferences SET enabled = ?, send_hour_utc = ?, updated_at = ?
           WHERE user_id = ?""",
        (int(bool(digest_enabled)), send_hour_utc, now, user["id"]),
    )
    await db.commit()
    return _redirect("/dashboard/settings?success=Notification+preferences+saved")


@router.post("/dashboard/settings/rotate-key")
async def rotate_key(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    new_key = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE users SET api_key = ?, api_key_hash = ?, updated_at = ? WHERE id = ?",
        (encrypt_value(new_key), hash_api_key(new_key), now, user["id"]),
    )
    await db.commit()
    response = _redirect("/login")
    clear_session(response)
    return response


@router.post("/dashboard/settings/delete-account")
async def delete_account(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    db = await get_database()
    await db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    await db.commit()
    response = _redirect("/")
    clear_session(response)
    return response


# ---------------------------------------------------------------------------
# Public status page (Tailwind version replaces inline HTML)
# ---------------------------------------------------------------------------

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
        if not mwc.is_public:
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
                {"ms": r["response_time_ms"], "height": max(5, int(r["response_time_ms"] / max_ms * 100))}
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

    # Overall status
    if not view_monitors:
        overall_class, overall_label = "operational", "All systems operational"
    else:
        statuses = [m["current_status"] for m in view_monitors]
        down_count = statuses.count("down") + statuses.count("error")
        if down_count == 0:
            overall_class, overall_label = "operational", "All systems operational"
        elif down_count == len(statuses):
            overall_class, overall_label = "major", "Major outage"
        else:
            overall_class, overall_label = "partial", "Partial outage"

    session_user = await _get_ui_user(request)
    return templates.TemplateResponse("status.html", {
        "request": request,
        "user": session_user,
        "user_id": user_id,
        "monitors": view_monitors,
        "overall_class": overall_class,
        "overall_label": overall_label,
    })
