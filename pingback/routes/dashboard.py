"""Server-rendered dashboard UI routes (Jinja2 + Tailwind)."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pingback.auth import _lookup_user, hash_api_key, hash_email
from pingback.config import APP_BASE_URL
from pingback.db.connection import get_database
from pingback.db.monitors import (
    count_user_monitors,
    create_monitor,
    delete_monitor,
    find_monitor_by_id,
    find_monitors_with_last_check,
    get_30day_uptime,
    get_check_history,
    get_response_times,
)
from pingback.encryption import encrypt_value
from pingback.services.plans import (
    PlanLimitExceeded,
    ensure_interval_allowed,
    ensure_monitor_quota,
)
from pingback.session import (
    clear_session,
    clear_signup_reveal,
    get_session_key,
    has_signup_reveal,
    set_session,
    set_signup_reveal,
)

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
    return templates.TemplateResponse(request, "landing.html", {"user": user})


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    user = await _get_ui_user(request)
    return templates.TemplateResponse(request, "pricing.html", {"user": user})


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    user = await _get_ui_user(request)
    return templates.TemplateResponse(request, "terms.html", {"user": user})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    user = await _get_ui_user(request)
    return templates.TemplateResponse(request, "privacy.html", {"user": user})


@router.get("/refund", response_class=HTMLResponse)
async def refund(request: Request):
    user = await _get_ui_user(request)
    return templates.TemplateResponse(request, "refund.html", {"user": user})


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await _get_ui_user(request)
    if user:
        return _redirect("/dashboard")
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, api_key: str = Form(...)):
    user = await _lookup_user(api_key)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html",
            {"user": None, "error": "Invalid API key. Check your key and try again."},
            status_code=401,
        )
    response = _redirect("/dashboard")
    set_session(response, api_key)
    return response


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    user = await _get_ui_user(request)
    upgrade = request.query_params.get("upgrade", "")
    if user:
        if upgrade == "pro" and user.get("plan") == "free":
            return _redirect("/pricing")
        return _redirect("/dashboard")
    return templates.TemplateResponse(
        request, "signup.html",
        {"user": None, "error": None, "email": "", "name": "", "upgrade": upgrade},
    )


@router.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    upgrade: str = Form(""),
):
    db = await get_database()

    # Dedup by deterministic email hash — Fernet encryption is non-deterministic
    # so a UNIQUE index on the encrypted `email` column does not catch dupes.
    email_hash_value = hash_email(email)
    async with db.execute(
        "SELECT id FROM users WHERE email_hash = ?", (email_hash_value,)
    ) as cur:
        if await cur.fetchone():
            return templates.TemplateResponse(
                request, "signup.html",
                {
                    "user": None,
                    "error": "An account with that email already exists.",
                    "email": email,
                    "name": name,
                    "upgrade": upgrade,
                },
                status_code=409,
            )

    user_id = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO users (id, email, email_hash, name, plan, api_key, api_key_hash, created_at, updated_at, last_login_at)
           VALUES (?, ?, ?, ?, 'free', ?, ?, ?, ?, ?)""",
        (user_id, encrypt_value(email), email_hash_value, name or None, encrypt_value(api_key), hash_api_key(api_key), now, now, now),
    )

    # Create default digest preferences
    unsub_token = secrets.token_urlsafe(16)
    await db.execute(
        """INSERT INTO digest_preferences (user_id, enabled, send_hour_utc, unsubscribe_token, created_at, updated_at)
           VALUES (?, 1, 8, ?, ?, ?)""",
        (user_id, unsub_token, now, now),
    )
    await db.commit()

    # Route through the one-time key reveal page so the user has a chance to
    # copy/save the plaintext API key before they leave the tab. Preserve the
    # upgrade flag so the Continue button lands them where they expected.
    reveal_url = "/signup/success"
    if upgrade == "pro":
        reveal_url += "?upgrade=pro"
    response = _redirect(reveal_url)
    set_session(response, api_key)
    set_signup_reveal(response)
    # Never cache the post-signup response — the Set-Cookie carries the
    # session and the reveal flag.
    response.headers["Cache-Control"] = "no-store, private"
    return response


@router.get("/signup/success", response_class=HTMLResponse)
async def signup_success(request: Request):
    """One-time reveal of the plaintext API key after signup.

    Gated on a short-lived `pb_signup_reveal` cookie plus the normal session
    cookie. The plaintext key is read back from the signed session cookie and
    rendered into the page exactly once per reveal window — it is never logged.
    """
    api_key = get_session_key(request)
    if not (api_key and has_signup_reveal(request)):
        return _redirect("/dashboard")
    user = await _lookup_user(api_key)
    if user is None:
        return _redirect("/dashboard")

    upgrade = request.query_params.get("upgrade", "")
    continue_url = "/pricing?signed_up=1" if upgrade == "pro" else "/dashboard?welcome=1"

    response = templates.TemplateResponse(request, "signup_success.html", {
        "user": user,
        "api_key": api_key,
        "email": user["email"],
        "upgrade": upgrade,
        "continue_url": continue_url,
    })
    response.headers["Cache-Control"] = "no-store, private"
    # Cookie is cleared when the user hits Continue (POST /signup/continue),
    # or naturally after the 10-minute TTL — whichever comes first. We keep it
    # around here so a quick refresh still shows the key during that window.
    return response


@router.post("/signup/continue")
async def signup_continue(request: Request, upgrade: str = Form("")):
    """Acknowledge the one-time API key reveal and land the user.

    Clears the reveal marker so subsequent /signup/success hits redirect to
    the dashboard — the key has been seen and should not be re-rendered.
    """
    target = "/pricing?signed_up=1" if upgrade == "pro" else "/dashboard?welcome=1"
    response = _redirect(target)
    clear_signup_reveal(response)
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
    latencies: list[int] = []
    uptimes: list[float] = []
    for mwc in monitors_raw:
        uptime = await get_30day_uptime(db, mwc.id)
        uptimes.append(uptime)
        current_status = "unknown"
        last_response_ms = None
        last_checked = None
        sparkline = None
        if mwc.last_check:
            current_status = mwc.last_check.status
            last_response_ms = mwc.last_check.response_time_ms
            last_checked = mwc.last_check.checked_at
        if last_response_ms is not None:
            latencies.append(last_response_ms)
        if current_status in ("down", "error"):
            down_count += 1

        # Sparkline from the 20 most recent samples (server-rendered polyline points)
        rts = await get_response_times(db, mwc.id, limit=20)
        if rts:
            vals = [r["response_time_ms"] for r in rts if r["response_time_ms"] is not None]
            if len(vals) >= 2:
                mx, mn = max(vals) or 1, min(vals)
                rng = (mx - mn) or 1
                w, h = 220.0, 24.0
                step = w / (len(vals) - 1)
                pts = [f"{i * step:.1f},{h - ((v - mn) / rng) * (h - 2) - 1:.1f}" for i, v in enumerate(vals)]
                sparkline = " ".join(pts)

        monitors.append({
            "id": mwc.id,
            "name": mwc.name,
            "url": mwc.url,
            "interval_seconds": mwc.interval_seconds,
            "current_status": current_status,
            "uptime": uptime,
            "last_response_ms": last_response_ms,
            "last_checked": last_checked,
            "sparkline": sparkline,
        })

    welcome = request.query_params.get("welcome") == "1" and not monitors

    overall_uptime = round(sum(uptimes) / len(uptimes), 2) if uptimes else None
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else None

    # Heatmap summary: keep lightweight — mark today's cell worst-of-all.
    cells = [""] * 90
    if monitors:
        worst = "up"
        for m in monitors:
            s = m["current_status"]
            if s == "down":
                worst = "down"
                break
            if s == "error" and worst == "up":
                worst = "deg"
        cells[-1] = {"up": "", "deg": "deg", "down": "down"}.get(worst, "")

    total_checks_24h: int | None = None
    try:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM checks c "
            "JOIN monitors m ON m.id = c.monitor_id "
            "WHERE m.user_id = ? AND c.checked_at >= datetime('now', '-1 day')",
            (user["id"],),
        ) as cur:
            row = await cur.fetchone()
            if row:
                total_checks_24h = row["n"]
    except Exception:
        total_checks_24h = None

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "monitors": monitors,
        "down_count": down_count,
        "welcome": welcome,
        "overall_uptime": overall_uptime,
        "avg_latency": avg_latency,
        "heatmap_cells": cells,
        "total_checks_24h": total_checks_24h,
    })


# ---------------------------------------------------------------------------
# Add / Edit monitor
# ---------------------------------------------------------------------------

@router.get("/dashboard/monitors/new", response_class=HTMLResponse)
async def new_monitor_page(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    return templates.TemplateResponse(request, "monitor_form.html", {
        "user": user, "monitor": None, "error": None, "name": "", "url": "",
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

    plan = user.get("plan", "free")
    try:
        ensure_monitor_quota(plan, await count_user_monitors(db, user["id"]))
        ensure_interval_allowed(plan, interval_seconds)
    except PlanLimitExceeded as exc:
        return templates.TemplateResponse(request, "monitor_form.html", {
            "user": user, "monitor": None,
            "error": exc.message,
            "upgrade_required": plan == "free",
            "name": name, "url": url,
        }, status_code=403)

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
    return templates.TemplateResponse(request, "monitor_form.html", {
        "user": user, "monitor": monitor, "error": None,
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
    plan = user.get("plan", "free")
    try:
        ensure_interval_allowed(plan, interval_seconds)
    except PlanLimitExceeded as exc:
        return templates.TemplateResponse(request, "monitor_form.html", {
            "user": user, "monitor": monitor,
            "error": exc.message,
            "upgrade_required": plan == "free",
        }, status_code=403)
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

    return templates.TemplateResponse(request, "monitor_detail.html", {
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

    return templates.TemplateResponse(request, "settings.html", {
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
    return templates.TemplateResponse(request, "status.html", {
        "user": session_user,
        "user_id": user_id,
        "monitors": view_monitors,
        "overall_class": overall_class,
        "overall_label": overall_label,
    })
