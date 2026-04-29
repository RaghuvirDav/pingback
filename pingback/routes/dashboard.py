"""Server-rendered dashboard UI routes (Jinja2 + Tailwind)."""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pingback.rate_limit import (
    require_forgot_rate_limit,
    require_login_rate_limit,
    require_reset_rate_limit,
    require_signup_rate_limit,
)
from pingback.auth import (
    MIN_PASSWORD_LENGTH,
    RESET_TTL_HOURS,
    VERIFICATION_TTL_HOURS,
    _lookup_user,
    generate_token,
    hash_api_key,
    hash_email,
    hash_password,
    is_token_expired,
    lookup_user_by_email,
    token_expiry,
    verify_password,
)
from pingback.config import APP_BASE_URL
from pingback.csrf import csrf_protect, register_csrf_globals
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
from pingback.encryption import decrypt_value, encrypt_value
from pingback.services.email import (
    send_password_reset_email,
    send_verification_email,
)
from pingback.services.plans import (
    PlanLimitExceeded,
    allowed_intervals_for_plan,
    ensure_interval_allowed,
    ensure_monitor_quota,
    limits_for,
    min_interval_for_plan,
)
from pingback.session import clear_session, get_session_key, set_session

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_csrf_globals(templates)

logger = logging.getLogger("pingback.dashboard")

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


# A curated short list keeps the dropdown usable. Anything not in this list
# can still be persisted via the API; the picker is just for the common case.
_COMMON_DIGEST_TIMEZONES = [
    "Etc/UTC",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Africa/Lagos",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
]


def _digest_timezone_options(current: str | None) -> list[str]:
    """Return the dropdown list, ensuring the user's current zone is included
    even if it's not on the curated list (so legacy/edge values still round-trip)."""
    options = list(_COMMON_DIGEST_TIMEZONES)
    if current and current not in options and current in available_timezones():
        options.append(current)
    return options


def _interval_choices_for(plan: str | None) -> tuple[list[dict], int]:
    """Return ([{seconds, label}, ...], floor_seconds) for the monitor form."""
    def label(s: int) -> str:
        if s < 60:
            return f"{s}s"
        if s < 3600:
            m = s // 60
            return f"{m}m" if s % 60 == 0 else f"{m}m {s % 60}s"
        h = s // 3600
        return f"{h}h" if s % 3600 == 0 else f"{h}h {(s % 3600) // 60}m"

    return (
        [{"seconds": s, "label": label(s)} for s in allowed_intervals_for_plan(plan)],
        min_interval_for_plan(plan),
    )


def _paddle_checkout_ctx() -> dict:
    """Paddle.Checkout.open inputs reused across pages that fire inline upgrades."""
    from pingback.config import (
        MAX_MONITORS_PRO,
        HISTORY_DAYS_PRO,
        PADDLE_CLIENT_TOKEN,
        PADDLE_DISCOUNT_ID_LAUNCH,
        PADDLE_ENVIRONMENT,
        PADDLE_PRICE_ID_MONTHLY,
        PADDLE_PRICE_ID_YEARLY,
    )
    return {
        "paddle_client_token": PADDLE_CLIENT_TOKEN,
        "paddle_environment": PADDLE_ENVIRONMENT,
        "paddle_price_monthly": PADDLE_PRICE_ID_MONTHLY,
        "paddle_price_yearly": PADDLE_PRICE_ID_YEARLY,
        "paddle_discount_launch": PADDLE_DISCOUNT_ID_LAUNCH,
        "app_base_url": APP_BASE_URL,
        "pro_max_monitors": MAX_MONITORS_PRO,
        "pro_history_days": HISTORY_DAYS_PRO,
    }


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    user = await _get_ui_user(request)
    return templates.TemplateResponse(request, "landing.html", {"user": user})


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    from pingback.config import (
        PADDLE_CLIENT_TOKEN,
        PADDLE_DISCOUNT_ID_LAUNCH,
        PADDLE_ENVIRONMENT,
        PADDLE_PRICE_ID_MONTHLY,
        PADDLE_PRICE_ID_YEARLY,
    )
    user = await _get_ui_user(request)
    return templates.TemplateResponse(request, "pricing.html", {
        "user": user,
        "paddle_client_token": PADDLE_CLIENT_TOKEN,
        "paddle_environment": PADDLE_ENVIRONMENT,
        "paddle_price_monthly": PADDLE_PRICE_ID_MONTHLY,
        "paddle_price_yearly": PADDLE_PRICE_ID_YEARLY,
        "paddle_discount_launch": PADDLE_DISCOUNT_ID_LAUNCH,
        "app_base_url": APP_BASE_URL,
    })


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
# Auth pages — email + password (MAK-96).
#
# API keys still exist and still authenticate the JSON API via Bearer token
# (see `auth.get_current_user`). They are no longer the UI sign-in credential.
# ---------------------------------------------------------------------------

_GENERIC_LOGIN_ERROR = "Invalid email or password."


def _verify_url(token: str) -> str:
    return f"{APP_BASE_URL}/verify?token={token}"


def _reset_url(token: str) -> str:
    return f"{APP_BASE_URL}/reset-password?token={token}"


async def _issue_verification_token(db, user_id: str) -> str:
    """Generate + persist a fresh verification token. Returns the plaintext token."""
    token = generate_token()
    await db.execute(
        "UPDATE users SET verification_token = ?, verification_expires_at = ?, updated_at = ? WHERE id = ?",
        (token, token_expiry(VERIFICATION_TTL_HOURS), datetime.now(timezone.utc).isoformat(), user_id),
    )
    await db.commit()
    return token


async def _issue_reset_token(db, user_id: str) -> str:
    token = generate_token()
    await db.execute(
        "UPDATE users SET reset_token = ?, reset_expires_at = ?, updated_at = ? WHERE id = ?",
        (token, token_expiry(RESET_TTL_HOURS), datetime.now(timezone.utc).isoformat(), user_id),
    )
    await db.commit()
    return token


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await _get_ui_user(request)
    if user:
        return _redirect("/dashboard")
    return templates.TemplateResponse(
        request, "login.html",
        {"user": None, "error": None, "notice": request.query_params.get("notice"), "email": ""},
    )


@router.post(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect), Depends(require_login_rate_limit)],
)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    user = await lookup_user_by_email(email)

    # Unknown account: show a generic error — don't leak which emails exist.
    if user is None:
        return templates.TemplateResponse(
            request, "login.html",
            {"user": None, "error": _GENERIC_LOGIN_ERROR, "notice": None, "email": email},
            status_code=401,
        )

    # Legacy accounts predating MAK-96 have no password yet. Email them a
    # set-password link (same mechanism as forgot-password) and tell them to
    # check their inbox — works even when the user forgot the migration lived.
    if not user["password_hash"]:
        db = await get_database()
        reset_token = await _issue_reset_token(db, user["id"])
        try:
            send_password_reset_email(to=user["email"], name=user["name"], reset_url=_reset_url(reset_token))
        except Exception:
            pass  # best-effort; token still valid if email transport blips
        return templates.TemplateResponse(
            request, "login.html",
            {
                "user": None, "error": None,
                "notice": "This account needs a password. We emailed you a link to set one — check your inbox.",
                "email": email,
            },
            status_code=200,
        )

    if not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"user": None, "error": _GENERIC_LOGIN_ERROR, "notice": None, "email": email},
            status_code=401,
        )

    # Correct password but email isn't verified yet — don't log them in.
    if not user["email_verified"]:
        db = await get_database()
        fresh_token = await _issue_verification_token(db, user["id"])
        try:
            send_verification_email(to=user["email"], name=user["name"], verify_url=_verify_url(fresh_token))
        except Exception:
            pass
        return templates.TemplateResponse(
            request, "login.html",
            {
                "user": None, "error": None,
                "notice": "Please verify your email. We just sent you a new verification link.",
                "email": email,
            },
            status_code=200,
        )

    api_key = decrypt_value(user["api_key_encrypted"])
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


@router.post(
    "/signup",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect), Depends(require_signup_rate_limit)],
)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    upgrade: str = Form(""),
):
    if len(password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            request, "signup.html",
            {
                "user": None,
                "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                "email": email, "name": name, "upgrade": upgrade,
            },
            status_code=400,
        )

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
                    "email": email, "name": name, "upgrade": upgrade,
                },
                status_code=409,
            )

    user_id = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    verification_token = generate_token()

    # Signup is the GDPR consent moment for the daily digest. Without this,
    # the consent filter in `get_users_due_for_digest` quietly drops every
    # newly created account (MAK-124).
    await db.execute(
        """INSERT INTO users (
               id, email, email_hash, name, plan,
               api_key, api_key_hash,
               password_hash, email_verified,
               verification_token, verification_expires_at,
               created_at, updated_at, last_login_at,
               consent_given_at, timezone
           ) VALUES (?, ?, ?, ?, 'free', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'Etc/UTC')""",
        (
            user_id, encrypt_value(email), email_hash_value, name or None,
            encrypt_value(api_key), hash_api_key(api_key),
            hash_password(password),
            verification_token, token_expiry(VERIFICATION_TTL_HOURS),
            now, now, now,
            now,
        ),
    )

    # Create default digest preferences
    unsub_token = secrets.token_urlsafe(16)
    await db.execute(
        """INSERT INTO digest_preferences (user_id, enabled, send_hour_utc, unsubscribe_token, created_at, updated_at)
           VALUES (?, 1, 8, ?, ?, ?)""",
        (user_id, unsub_token, now, now),
    )
    await db.commit()

    verify_link = _verify_url(verification_token)
    if upgrade == "pro":
        verify_link += "&upgrade=pro"
    try:
        send_verification_email(to=email, name=name or None, verify_url=verify_link)
    except Exception:
        pass  # best-effort; user can request a resend from /login or /verify/resend

    # Do NOT log the user in — they must click the verification link first.
    return templates.TemplateResponse(
        request, "signup_success.html",
        {
            "user": None,
            "email": email,
            "api_key": api_key,  # one-time reveal — never shown again
            "upgrade": upgrade,
        },
    )


@router.get("/verify", response_class=HTMLResponse)
async def verify_email(request: Request):
    token = request.query_params.get("token", "").strip()
    if not token:
        return templates.TemplateResponse(
            request, "verify_email.html",
            {"user": None, "ok": False, "message": "Missing verification token."},
            status_code=400,
        )

    db = await get_database()
    async with db.execute(
        """SELECT id, email, name, api_key, verification_expires_at, email_verified
           FROM users WHERE verification_token = ?""",
        (token,),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        return templates.TemplateResponse(
            request, "verify_email.html",
            {"user": None, "ok": False, "message": "Invalid or already-used verification link."},
            status_code=400,
        )

    # Token already consumed (email_verified=1 and token column cleared on success).
    if is_token_expired(row["verification_expires_at"]):
        return templates.TemplateResponse(
            request, "verify_email.html",
            {"user": None, "ok": False, "message": "This verification link has expired. Sign in and we'll email you a new one."},
            status_code=400,
        )

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE users SET email_verified = 1,
                            verification_token = NULL, verification_expires_at = NULL,
                            updated_at = ?, last_login_at = ?
           WHERE id = ?""",
        (now, now, row["id"]),
    )
    await db.commit()

    # Log them in immediately — the verification click IS the login. Honour
    # the upgrade=pro intent from the signup form so Pro signups land on the
    # pricing page ready to checkout.
    upgrade = request.query_params.get("upgrade", "")
    landing = "/pricing?signed_up=1" if upgrade == "pro" else "/dashboard?welcome=1"
    api_key = decrypt_value(row["api_key"])
    response = _redirect(landing)
    set_session(response, api_key)
    return response


@router.post(
    "/verify/resend",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect)],
)
async def resend_verification(request: Request, email: str = Form(...)):
    """Send a new verification link by email. Never leaks whether the account exists."""
    user = await lookup_user_by_email(email)
    if user and not user["email_verified"]:
        db = await get_database()
        fresh_token = await _issue_verification_token(db, user["id"])
        try:
            send_verification_email(to=user["email"], name=user["name"], verify_url=_verify_url(fresh_token))
        except Exception:
            pass
    return _redirect("/login?notice=If+the+account+needs+verification%2C+we+sent+a+new+link.")


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    if await _get_ui_user(request):
        return _redirect("/dashboard/settings")
    return templates.TemplateResponse(
        request, "forgot_password.html",
        {"user": None, "notice": None, "error": None},
    )


@router.post(
    "/forgot-password",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect), Depends(require_forgot_rate_limit)],
)
async def forgot_password_submit(request: Request, email: str = Form(...)):
    user = await lookup_user_by_email(email)
    # Always show the same confirmation — never leak which emails have accounts.
    if user is not None:
        db = await get_database()
        reset_token = await _issue_reset_token(db, user["id"])
        try:
            send_password_reset_email(to=user["email"], name=user["name"], reset_url=_reset_url(reset_token))
        except Exception:
            pass
    return templates.TemplateResponse(
        request, "forgot_password.html",
        {
            "user": None,
            "notice": "If an account exists for that address, we just emailed a reset link. Check your inbox.",
            "error": None,
        },
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    token = request.query_params.get("token", "").strip()
    if not token:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"user": None, "ok": False, "error": "Missing token.", "token": ""},
            status_code=400,
        )

    db = await get_database()
    async with db.execute(
        "SELECT id, reset_expires_at FROM users WHERE reset_token = ?", (token,)
    ) as cur:
        row = await cur.fetchone()
    if row is None or is_token_expired(row["reset_expires_at"]):
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"user": None, "ok": False, "error": "This reset link is invalid or has expired.", "token": ""},
            status_code=400,
        )
    return templates.TemplateResponse(
        request, "reset_password.html",
        {"user": None, "ok": True, "error": None, "token": token},
    )


@router.post(
    "/reset-password",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect), Depends(require_reset_rate_limit)],
)
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
):
    if len(password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {
                "user": None, "ok": True,
                "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                "token": token,
            },
            status_code=400,
        )

    db = await get_database()
    async with db.execute(
        "SELECT id, email, name, api_key, reset_expires_at FROM users WHERE reset_token = ?",
        (token,),
    ) as cur:
        row = await cur.fetchone()
    if row is None or is_token_expired(row["reset_expires_at"]):
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"user": None, "ok": False, "error": "This reset link is invalid or has expired.", "token": ""},
            status_code=400,
        )

    now = datetime.now(timezone.utc).isoformat()
    # Reset also implicitly verifies email: the user just demonstrated control
    # of the inbox by clicking the link.
    await db.execute(
        """UPDATE users SET password_hash = ?,
                            email_verified = 1,
                            reset_token = NULL, reset_expires_at = NULL,
                            verification_token = NULL, verification_expires_at = NULL,
                            updated_at = ?, last_login_at = ?
           WHERE id = ?""",
        (hash_password(password), now, now, row["id"]),
    )
    await db.commit()

    api_key = decrypt_value(row["api_key"])
    response = _redirect("/dashboard")
    set_session(response, api_key)
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect)])
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
    incidents = []
    down_count = 0
    err_count = 0
    latencies: list[int] = []
    uptimes: list[float] = []
    for mwc in monitors_raw:
        uptime = await get_30day_uptime(db, mwc.id)
        uptimes.append(uptime)
        current_status = "unknown"
        last_response_ms = None
        last_checked = None
        last_status_code = None
        last_error = None
        sparkline = None
        if mwc.last_check:
            current_status = mwc.last_check.status
            last_response_ms = mwc.last_check.response_time_ms
            last_checked = mwc.last_check.checked_at
            last_status_code = mwc.last_check.status_code
            last_error = mwc.last_check.error
        if last_response_ms is not None:
            latencies.append(last_response_ms)
        if current_status == "down":
            down_count += 1
        elif current_status == "error":
            err_count += 1
        if current_status in ("down", "error"):
            incidents.append({
                "id": mwc.id,
                "name": mwc.name,
                "url": mwc.url,
                "status": current_status,
                "status_code": last_status_code,
                "error": last_error,
                "checked_at": last_checked,
            })

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

    total_checks_24h: int | None = None
    try:
        from pingback.db.rollups import count_user_checks_in_window
        total_checks_24h = await count_user_checks_in_window(db, user["id"], 24 * 3600)
    except Exception:
        logger.exception("dashboard checks_24h query failed")
        total_checks_24h = None

    # 90-day heatmap: only render cells once we have ≥1 day of real check
    # history. Until then, show the "Building 90-day history…" placeholder so
    # we don't fabricate a green wall before any check has run (MAK-162).
    has_history = False
    if monitors:
        async with db.execute(
            """SELECT 1 FROM check_results c
               JOIN monitors m ON m.id = c.monitor_id
               WHERE m.user_id = ?
                 AND c.checked_at <= datetime('now', '-1 day')
               LIMIT 1""",
            (user["id"],),
        ) as cur:
            has_history = await cur.fetchone() is not None

    cells: list[str] = []
    if has_history:
        # Pre-fill missing buckets with "empty" (neutral) — never green —
        # then mark today's cell with the worst-of-all monitor status.
        cells = ["empty"] * 90
        worst = "up"
        for m in monitors:
            s = m["current_status"]
            if s == "down":
                worst = "down"
                break
            if s == "error" and worst == "up":
                worst = "deg"
        cells[-1] = {"up": "", "deg": "deg", "down": "down"}.get(worst, "empty")

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "monitors": monitors,
        "incidents": incidents,
        "down_count": down_count,
        "err_count": err_count,
        "welcome": welcome,
        "overall_uptime": overall_uptime,
        "avg_latency": avg_latency,
        "heatmap_cells": cells,
        "heatmap_has_history": has_history,
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
    plan = user.get("plan", "free")
    intervals, floor = _interval_choices_for(plan)
    upgraded = request.query_params.get("upgraded") == "1"
    return templates.TemplateResponse(request, "monitor_form.html", {
        "user": user, "monitor": None, "error": None, "name": "", "url": "",
        "allowed_intervals": intervals, "plan_floor_seconds": floor,
        "upgraded_resume": upgraded,
    })


@router.post(
    "/dashboard/monitors/new",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect)],
)
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
    current_count = await count_user_monitors(db, user["id"])
    cap_kind: str | None = None
    try:
        ensure_monitor_quota(plan, current_count)
    except PlanLimitExceeded as exc:
        cap_kind = "monitors"
        cap_message = exc.message
    if cap_kind is None:
        try:
            ensure_interval_allowed(plan, interval_seconds)
        except PlanLimitExceeded as exc:
            cap_kind = "interval"
            cap_message = exc.message
    if cap_kind is not None:
        intervals, floor = _interval_choices_for(plan)
        ctx = {
            "user": user, "monitor": None,
            "error": cap_message,
            "upgrade_required": plan == "free",
            "cap_hit": plan == "free",
            "cap_hit_kind": cap_kind,
            "current_monitor_count": current_count,
            "current_monitor_limit": limits_for(plan).max_monitors,
            "name": name, "url": url, "interval_seconds": interval_seconds,
            "is_public": int(bool(is_public)),
            "allowed_intervals": intervals, "plan_floor_seconds": floor,
        }
        ctx.update(_paddle_checkout_ctx())
        return templates.TemplateResponse(request, "monitor_form.html", ctx, status_code=403)

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
    plan = user.get("plan", "free")
    intervals, floor = _interval_choices_for(plan)
    return templates.TemplateResponse(request, "monitor_form.html", {
        "user": user, "monitor": monitor, "error": None,
        "allowed_intervals": intervals, "plan_floor_seconds": floor,
    })


@router.post(
    "/dashboard/monitors/{monitor_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect)],
)
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
        intervals, floor = _interval_choices_for(plan)
        ctx = {
            "user": user, "monitor": monitor,
            "error": exc.message,
            "upgrade_required": plan == "free",
            "cap_hit": plan == "free",
            "cap_hit_kind": "interval",
            "allowed_intervals": intervals, "plan_floor_seconds": floor,
        }
        ctx.update(_paddle_checkout_ctx())
        return templates.TemplateResponse(request, "monitor_form.html", ctx, status_code=403)
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE monitors SET name = ?, url = ?, interval_seconds = ?, is_public = ?, updated_at = ?
           WHERE id = ?""",
        (name, url, interval_seconds, int(bool(is_public)), now, monitor_id),
    )
    await db.commit()
    return _redirect(f"/dashboard/monitors/{monitor_id}")


@router.post(
    "/dashboard/monitors/{monitor_id}/delete",
    dependencies=[Depends(csrf_protect)],
)
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
    last_error = None
    if checks:
        current_status = checks[0].status
        last_response_ms = checks[0].response_time_ms
        for c in checks:
            if c.error:
                last_error = c.error
                break

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
        "last_error": last_error,
        "checks": checks,
        "checks_24h": len(checks),
        "has_history": bool(checks),
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

    user_timezone = user.get("timezone") or "Etc/UTC"
    status_url = f"{APP_BASE_URL}/status/{user['id']}"

    return templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "user_timezone": user_timezone,
        "status_url": status_url,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post(
    "/dashboard/settings/notifications",
    dependencies=[Depends(csrf_protect)],
)
async def update_notifications(
    request: Request,
    digest_enabled: int = Form(0),
    timezone_name: str = Form("Etc/UTC"),
    redirect_to: str = Form("/dashboard/billing"),
):
    """Update digest enabled + timezone. Send hour is locked at 08:00 local
    (MAK-126) — no per-user override."""
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    # MAK-164: canonicalise deprecated tzdata alias on settings save too.
    if timezone_name == "Asia/Calcutta":
        timezone_name = "Asia/Kolkata"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return _redirect(f"{redirect_to}?error=Unknown+timezone")

    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE users SET timezone = ?, updated_at = ?,
               consent_given_at = COALESCE(consent_given_at, ?)
           WHERE id = ?""",
        (timezone_name, now, now if digest_enabled else None, user["id"]),
    )
    await db.execute(
        """UPDATE digest_preferences SET enabled = ?, send_hour_utc = 8, updated_at = ?
           WHERE user_id = ?""",
        (int(bool(digest_enabled)), now, user["id"]),
    )
    await db.commit()
    return _redirect(f"{redirect_to}?success=Notification+preferences+saved")


@router.post("/api/users/me/timezone")
async def update_my_timezone(request: Request):
    """Browser-detected timezone seed. Only overwrites the legacy `Etc/UTC`
    default — never clobbers an explicit user choice (MAK-126)."""
    user = await _get_ui_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    tz_name = (body or {}).get("timezone")
    if not isinstance(tz_name, str) or not tz_name:
        raise HTTPException(status_code=400, detail="Missing timezone")
    # MAK-164: canonicalise deprecated tzdata aliases on write so we don't
    # accumulate fresh `Asia/Calcutta` rows from older browsers/JS shims.
    if tz_name == "Asia/Calcutta":
        tz_name = "Asia/Kolkata"
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=400, detail="Unknown timezone")

    if (user.get("timezone") or "Etc/UTC") != "Etc/UTC":
        return {"updated": False, "timezone": user.get("timezone")}

    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ? AND timezone = 'Etc/UTC'",
        (tz_name, now, user["id"]),
    )
    await db.commit()
    return {"updated": True, "timezone": tz_name}


@router.post(
    "/dashboard/settings/change-password",
    dependencies=[Depends(csrf_protect)],
)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return _redirect(f"/dashboard/settings?error=Password+must+be+at+least+{MIN_PASSWORD_LENGTH}+characters.")

    full = await lookup_user_by_email(user["email"])
    if full is None or not verify_password(current_password, full["password_hash"]):
        return _redirect("/dashboard/settings?error=Current+password+is+incorrect.")

    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(new_password), now, user["id"]),
    )
    await db.commit()
    return _redirect("/dashboard/settings?success=Password+updated")


@router.post(
    "/dashboard/settings/resend-verification",
    dependencies=[Depends(csrf_protect)],
)
async def resend_verification_from_settings(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")
    if user.get("email_verified"):
        return _redirect("/dashboard/settings?success=Your+email+is+already+verified")

    db = await get_database()
    fresh_token = await _issue_verification_token(db, user["id"])
    try:
        send_verification_email(to=user["email"], name=user["name"], verify_url=_verify_url(fresh_token))
    except Exception:
        return _redirect("/dashboard/settings?error=Could+not+send+verification+email+right+now")
    return _redirect("/dashboard/settings?success=Verification+email+sent")


@router.post(
    "/dashboard/settings/rotate-key",
    dependencies=[Depends(csrf_protect)],
)
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


@router.post(
    "/dashboard/settings/delete-account",
    dependencies=[Depends(csrf_protect)],
)
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
