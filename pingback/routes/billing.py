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
from pingback.encryption import decrypt_value
from pingback.routes.dashboard import _digest_timezone_options, _get_ui_user, _redirect
from pingback.services.email import send_pro_welcome_email
from pingback.services.plans import min_interval_for_plan


def _interval_label(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "1 min" if minutes == 1 else f"{minutes} min"
    return f"{seconds} sec"

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
    floor_seconds = min_interval_for_plan(user.get("plan"))

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
        "plan_floor_seconds": floor_seconds,
        "plan_floor_label": _interval_label(floor_seconds),
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
    await _send_pro_welcome_if_needed(data)


# ---------------------------------------------------------------------------
# Pro upgrade welcome / receipt email (MAK-111)
# ---------------------------------------------------------------------------

# Paddle minor-unit currencies — one display unit = 100 minor units. We don't
# need to be exhaustive: anything missing falls through to "skip the price
# string", which the email tolerates. Zero-decimal currencies (JPY, KRW) are
# whitelisted explicitly so we don't divide them by 100.
_ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "VND", "CLP", "PYG"}


def _format_amount(amount_str: str | None, currency: str | None) -> str | None:
    if not amount_str or not currency:
        return None
    try:
        minor = int(amount_str)
    except (TypeError, ValueError):
        return None
    currency = currency.upper()
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return f"{minor} {currency}"
    return f"{currency} {minor / 100:.2f}"


def _extract_plan_summary(data: dict) -> tuple[str | None, str | None]:
    """Build the (amount_display, billing_interval) strings shown in the email.

    Paddle's subscription.created payload has the pricing under
    items[].price.unit_price.{amount,currency_code} and the cadence under
    items[].price.billing_cycle.{interval,frequency}. We tolerate missing
    structure — the email body skips the price line if we can't extract it.
    """
    items = data.get("items") or []
    if not items:
        return None, None
    price = (items[0] or {}).get("price") or {}
    unit_price = price.get("unit_price") or {}
    amount = _format_amount(unit_price.get("amount"), unit_price.get("currency_code") or data.get("currency_code"))
    cycle = price.get("billing_cycle") or {}
    interval = cycle.get("interval")
    frequency = cycle.get("frequency", 1)
    if amount and interval:
        if frequency and frequency != 1:
            amount = f"{amount} every {frequency} {interval}s"
        else:
            amount = f"{amount}/{interval}"
    return amount, interval


def _format_renewal_date(iso_value: str | None) -> str | None:
    if not iso_value:
        return None
    try:
        # Paddle sends RFC3339 like "2026-05-21T00:00:00Z"
        dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%b %d, %Y")


async def _send_pro_welcome_if_needed(data: dict) -> None:
    """Send the Pro welcome email exactly once per user.

    Idempotency layers:
      1. Webhook event_id dedup in paddle_events (caller-side).
      2. users.pro_welcome_sent_at timestamp set here — guards against Paddle
         emitting subscription.created twice with different event_ids
         (e.g., recreate-after-cancel) and against future code paths that
         might call this helper for non-create events.
    """
    customer_id = data.get("customer_id") or (data.get("customer") or {}).get("id")
    status = data.get("status")
    if not customer_id or status not in _PRO_STATUSES:
        return

    db = await get_database()
    async with db.execute(
        """SELECT id, email, name, pro_welcome_sent_at
             FROM users WHERE paddle_customer_id = ?""",
        (customer_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        logger.warning("Pro welcome email: no user for paddle_customer_id=%s", customer_id)
        return
    if row["pro_welcome_sent_at"]:
        logger.info("Pro welcome already sent for user %s — skipping", row["id"])
        return

    email_plain = decrypt_value(row["email"])
    amount_display, _interval = _extract_plan_summary(data)
    next_billed_display = _format_renewal_date(_next_renewal_iso(data))

    # Stamp first so a downstream send failure can't trigger a re-send loop on
    # webhook retries. Paddle will email its own invoice regardless, so a
    # missed welcome email is a soft failure — log + move on.
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE users SET pro_welcome_sent_at = ?, updated_at = ? WHERE id = ?",
        (now, now, row["id"]),
    )
    await db.commit()

    try:
        send_pro_welcome_email(
            to=email_plain,
            name=row["name"],
            amount_display=amount_display,
            next_billed_display=next_billed_display,
        )
    except Exception:
        logger.exception("Pro welcome email send failed for user %s", row["id"])


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
