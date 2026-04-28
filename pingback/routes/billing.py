"""Paddle billing routes: webhook, customer portal, and billing settings page.

Pivoted from Stripe to Paddle (MoR) — see MAK-82. Checkout itself runs
client-side via Paddle.js (`Paddle.Checkout.open(...)`) so there is no
server-side `/billing/checkout` endpoint; the webhook is the only authority
for plan state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pingback.config import (
    APP_BASE_URL,
    PADDLE_API_BASE_URL,
    PADDLE_API_KEY,
    PADDLE_CLIENT_TOKEN,
    PADDLE_DISCOUNT_ID_LAUNCH,
    PADDLE_ENVIRONMENT,
    PADDLE_PRICE_ID_MONTHLY,
    PADDLE_PRICE_ID_YEARLY,
    PADDLE_PRODUCT_ID,
    PADDLE_WEBHOOK_SECRET,
)
from pingback.db.connection import get_database
from pingback.routes.dashboard import _digest_timezone_options, _get_ui_user, _redirect

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()
logger = logging.getLogger("pingback.billing")


# ---------------------------------------------------------------------------
# Billing settings page
# ---------------------------------------------------------------------------

@router.get("/dashboard/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    db = await get_database()
    digest_enabled = True
    async with db.execute(
        "SELECT enabled FROM digest_preferences WHERE user_id = ?", (user["id"],)
    ) as cur:
        row = await cur.fetchone()
        if row:
            digest_enabled = bool(row["enabled"])

    user_timezone = user.get("timezone") or "Etc/UTC"

    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "paddle_client_token": PADDLE_CLIENT_TOKEN,
        "paddle_environment": PADDLE_ENVIRONMENT,
        "paddle_price_monthly": PADDLE_PRICE_ID_MONTHLY,
        "paddle_price_yearly": PADDLE_PRICE_ID_YEARLY,
        "paddle_discount_launch": PADDLE_DISCOUNT_ID_LAUNCH,
        "digest_enabled": digest_enabled,
        "user_timezone": user_timezone,
        "timezone_options": _digest_timezone_options(user_timezone),
        "app_base_url": APP_BASE_URL,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


# ---------------------------------------------------------------------------
# Customer portal — manage / cancel subscription
# ---------------------------------------------------------------------------

@router.post("/dashboard/billing/portal")
async def create_portal_session(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    customer_id = user.get("paddle_customer_id")
    if not customer_id:
        return _redirect("/dashboard/billing?error=No+billing+account+found")

    if not PADDLE_API_KEY:
        return _redirect("/dashboard/billing?error=Billing+is+not+configured")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{PADDLE_API_BASE_URL}/customers/{customer_id}/portal-sessions",
                headers={
                    "Authorization": f"Bearer {PADDLE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={},
            )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Paddle portal-session call failed for %s: %s", customer_id, exc)
        return _redirect("/dashboard/billing?error=Could+not+open+portal")

    portal_url = (
        resp.json()
        .get("data", {})
        .get("urls", {})
        .get("general", {})
        .get("overview")
    )
    if not portal_url:
        return _redirect("/dashboard/billing?error=Portal+URL+missing")

    return RedirectResponse(url=portal_url, status_code=303)


# ---------------------------------------------------------------------------
# Paddle webhook handler
# ---------------------------------------------------------------------------

@router.post("/api/paddle/webhook")
async def paddle_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("paddle-signature", "")

    if not PADDLE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Billing webhook is not configured")

    if not _verify_paddle_signature(payload, sig_header, PADDLE_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    event_id = event.get("event_id") or event.get("notification_id")
    event_type = event.get("event_type", "")
    if not event_id:
        raise HTTPException(status_code=400, detail="Webhook event missing id")

    if await _already_processed(event_id):
        logger.info("Duplicate Paddle event %s (%s) ignored", event_id, event_type)
        return {"received": True, "duplicate": True}

    handler = _WEBHOOK_HANDLERS.get(event_type)
    if handler:
        await handler(event.get("data", {}))

    await _record_event(event_id, event_type)
    return {"received": True}


# ---------------------------------------------------------------------------
# Signature verification — Paddle-Signature: ts=...;h1=...
# Signed payload = f"{ts}:{raw_body}", HMAC-SHA256 with PADDLE_WEBHOOK_SECRET.
# https://developer.paddle.com/webhooks/signature-verification
# ---------------------------------------------------------------------------

def _verify_paddle_signature(payload: bytes, header: str, secret: str) -> bool:
    if not header:
        return False
    parts: dict[str, str] = {}
    for chunk in header.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    ts = parts.get("ts")
    h1 = parts.get("h1")
    if not ts or not h1:
        return False
    try:
        signed_payload = f"{ts}:{payload.decode('utf-8')}".encode("utf-8")
    except UnicodeDecodeError:
        return False
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

async def _already_processed(event_id: str) -> bool:
    db = await get_database()
    async with db.execute(
        "SELECT 1 FROM paddle_events WHERE id = ?", (event_id,)
    ) as cur:
        return (await cur.fetchone()) is not None


async def _record_event(event_id: str, event_type: str) -> None:
    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO paddle_events (id, type, received_at) VALUES (?, ?, ?)",
        (event_id, event_type, now),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------------------

async def _handle_subscription_created(data: dict) -> None:
    await _sync_subscription(data)


async def _handle_subscription_updated(data: dict) -> None:
    await _sync_subscription(data)


async def _handle_subscription_canceled(data: dict) -> None:
    """Subscription canceled. If `scheduled_change` is set Paddle keeps the
    user on Pro until that effective date — we stamp `plan_cancel_at` and
    leave plan='pro' until it elapses (handled in _sync_subscription)."""
    await _sync_subscription(data)


async def _handle_transaction_completed(data: dict) -> None:
    customer_id = (data.get("customer_id")) or (data.get("customer") or {}).get("id")
    logger.info("Paddle transaction completed for customer %s (txn %s)", customer_id, data.get("id"))


async def _handle_payment_failed(data: dict) -> None:
    """Paddle handles dunning + retries automatically; subscription.updated
    follows when status actually changes. Just log here."""
    customer_id = (data.get("customer_id")) or (data.get("customer") or {}).get("id")
    logger.warning("Paddle payment failed for customer %s (txn %s)", customer_id, data.get("id"))


# Local plan derivation. Paddle subscription statuses we care about:
#   active, trialing  -> pro
#   past_due, paused  -> stay pro until next billing cycle, but don't extend
#   canceled          -> free immediately if no scheduled_change, else pro
#                        until scheduled_change.effective_at
_PRO_STATUSES = {"active", "trialing", "past_due", "paused"}


def _next_renewal_iso(data: dict) -> str | None:
    return _normalize_iso(data.get("next_billed_at") or data.get("current_billing_period", {}).get("ends_at"))


def _normalize_iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
        except (OSError, ValueError):
            return None
    return str(value)


async def _sync_subscription(data: dict) -> None:
    customer_id = data.get("customer_id") or (data.get("customer") or {}).get("id")
    sub_id = data.get("id")
    status = data.get("status")
    if not customer_id:
        return

    scheduled = data.get("scheduled_change") or {}
    cancel_at = (
        _normalize_iso(scheduled.get("effective_at"))
        if scheduled.get("action") == "cancel"
        else None
    )
    next_renewal = _next_renewal_iso(data)

    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()

    if status in _PRO_STATUSES or (status == "canceled" and cancel_at):
        await db.execute(
            """UPDATE users
                  SET plan = 'pro',
                      paddle_subscription_id = ?,
                      plan_renews_at = ?,
                      plan_cancel_at = ?,
                      updated_at = ?
                WHERE paddle_customer_id = ?""",
            (sub_id, next_renewal, cancel_at, now, customer_id),
        )
    else:
        # canceled (immediate), expired, etc. → free now.
        await db.execute(
            """UPDATE users
                  SET plan = 'free',
                      paddle_subscription_id = NULL,
                      plan_renews_at = NULL,
                      plan_cancel_at = NULL,
                      updated_at = ?
                WHERE paddle_customer_id = ?""",
            (now, customer_id),
        )

    await db.commit()
    logger.info(
        "Synced Paddle subscription %s (status=%s, cancel_at=%s) for customer %s",
        sub_id,
        status,
        cancel_at,
        customer_id,
    )


# Some events (subscription.created, transaction.completed) include enough
# context that we can claim the Paddle customer id for the local user even
# before the subscription is fully provisioned. The mapping comes from the
# `custom_data` blob the dashboard form sets when opening Paddle.Checkout.
async def _claim_customer_for_user(data: dict) -> None:
    customer_id = data.get("customer_id") or (data.get("customer") or {}).get("id")
    custom = data.get("custom_data") or {}
    pingback_user_id = custom.get("pingback_user_id")
    if not customer_id or not pingback_user_id:
        return
    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE users SET paddle_customer_id = ?, updated_at = ?
            WHERE id = ?
              AND (paddle_customer_id IS NULL OR paddle_customer_id = ?)""",
        (customer_id, now, pingback_user_id, customer_id),
    )
    await db.commit()


async def _handle_subscription_created_with_claim(data: dict) -> None:
    await _claim_customer_for_user(data)
    await _sync_subscription(data)


_WEBHOOK_HANDLERS = {
    "subscription.created": _handle_subscription_created_with_claim,
    "subscription.updated": _handle_subscription_updated,
    "subscription.canceled": _handle_subscription_canceled,
    "transaction.completed": _handle_transaction_completed,
    "transaction.payment_failed": _handle_payment_failed,
}


# Reference — used by templates and tests to confirm price IDs are loaded.
__all__ = [
    "router",
    "PADDLE_PRICE_ID_MONTHLY",
    "PADDLE_PRICE_ID_YEARLY",
    "PADDLE_PRODUCT_ID",
]
