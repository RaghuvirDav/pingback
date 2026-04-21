"""Paddle billing routes: settings page, webhook, portal redirect.

Paddle uses a client-side overlay checkout (Paddle.js `Checkout.open()`) —
there is no server-side `checkout.session.create` equivalent. The backend's
only jobs are to verify signed webhooks, keep the users row in sync with
subscription state, and redirect the customer into Paddle's managed portal
when they want to cancel or update payment.
"""
from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pingback.config import (
    APP_BASE_URL,
    PADDLE_NOTIFICATION_SECRET,
    paddle_template_context,
)
from pingback.db.connection import get_database
from pingback.routes.dashboard import _get_ui_user, _redirect
from pingback.services.plans import sync_from_paddle_event

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

    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        **paddle_template_context(),
        "checkout_success_url": f"{APP_BASE_URL}/dashboard/billing?success=Welcome+to+Pro!",
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


# ---------------------------------------------------------------------------
# Customer portal — redirect to Paddle's per-subscription portal URL.
#
# Paddle returns `customer_portal_url` on subscription creation; we cache it
# on the user row in the webhook handler, then 302 here on demand. This
# replaces Stripe's `billing_portal.Session.create` server call.
# ---------------------------------------------------------------------------

@router.get("/dashboard/billing/portal")
async def billing_portal(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    db = await get_database()
    async with db.execute(
        "SELECT paddle_portal_url FROM users WHERE id = ?", (user["id"],)
    ) as cur:
        row = await cur.fetchone()
    portal_url = row["paddle_portal_url"] if row else None
    if not portal_url:
        return _redirect("/dashboard/billing?error=No+billing+account+found")
    return RedirectResponse(url=portal_url, status_code=303)


# ---------------------------------------------------------------------------
# Paddle webhook handler
# ---------------------------------------------------------------------------

@router.post("/api/paddle/webhook")
async def paddle_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("paddle-signature", "")

    if not PADDLE_NOTIFICATION_SECRET:
        raise HTTPException(status_code=503, detail="Billing webhook is not configured")

    try:
        payload_str = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    if not _verify_paddle_signature(payload_str, sig_header, PADDLE_NOTIFICATION_SECRET):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    event_id = event.get("event_id")
    event_type = event.get("event_type", "")
    if not event_id:
        raise HTTPException(status_code=400, detail="Webhook event missing event_id")

    # INSERT OR IGNORE + rowcount check gives us atomic "first-time or duplicate"
    # detection in one round-trip. Paddle retries use the same event_id so a
    # second delivery hits the ignore path and returns without running handlers.
    if not await _claim_event(event_id, event_type):
        logger.info("Duplicate Paddle event %s (%s) ignored", event_id, event_type)
        return {"received": True, "duplicate": True}

    handler = _WEBHOOK_HANDLERS.get(event_type)
    if handler:
        await handler(event.get("data") or {})

    return {"received": True}


# ---------------------------------------------------------------------------
# Signature verification
#
# Paddle signs the raw request body with HMAC-SHA256. Header shape:
#   Paddle-Signature: ts=<unix-ts>;h1=<hex-digest>
# The signed payload is literally `"<ts>:<body>"`. We split the header, rebuild
# the signed string, and do a constant-time compare against the provided h1.
# ---------------------------------------------------------------------------

def _verify_paddle_signature(payload: str, header: str, secret: str) -> bool:
    if not header:
        return False
    parts = {}
    for kv in header.split(";"):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        parts[k.strip()] = v.strip()
    ts = parts.get("ts")
    h1 = parts.get("h1")
    if not ts or not h1:
        return False
    signed = f"{ts}:{payload}".encode()
    expected = hmac.new(secret.encode(), signed, sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

async def _claim_event(event_id: str, event_type: str) -> bool:
    """Return True if this event_id is new (and was just recorded), False if
    it's a retry of an already-processed delivery."""
    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO paddle_events (id, type, received_at) VALUES (?, ?, ?)",
        (event_id, event_type, now),
    )
    await db.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Webhook event handlers
#
# All subscription events route through `sync_from_paddle_event`, which is
# the single source of truth for mapping Paddle state → the local users row.
# `subscription.past_due` intentionally does NOT downgrade — Paddle retries
# the payment; we only flip to free on `subscription.canceled` (which fires
# after Paddle gives up or at period end).
# `transaction.payment_failed` is log-only — the follow-up `subscription.*`
# event is what actually changes plan state.
# ---------------------------------------------------------------------------

async def _log_payment_failed(data: dict) -> None:
    logger.warning(
        "Paddle payment failed for customer %s (transaction %s)",
        data.get("customer_id"), data.get("id"),
    )


_WEBHOOK_HANDLERS = {
    "transaction.payment_failed": _log_payment_failed,
    "subscription.created": sync_from_paddle_event,
    "subscription.updated": sync_from_paddle_event,
    "subscription.canceled": sync_from_paddle_event,
    "subscription.past_due": sync_from_paddle_event,
}
