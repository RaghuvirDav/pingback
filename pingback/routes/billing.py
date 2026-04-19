"""Stripe billing routes: checkout, webhooks, and billing settings page."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pingback.config import (
    APP_BASE_URL,
    STRIPE_PRO_PRICE_ID,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)
from pingback.db.connection import get_database
from pingback.routes.dashboard import _get_ui_user, _redirect

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()
logger = logging.getLogger("pingback.billing")

stripe.api_key = STRIPE_SECRET_KEY


# ---------------------------------------------------------------------------
# Billing settings page
# ---------------------------------------------------------------------------

@router.get("/dashboard/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    subscription = None
    if user.get("stripe_subscription_id") and STRIPE_SECRET_KEY:
        try:
            subscription = stripe.Subscription.retrieve(user["stripe_subscription_id"])
        except stripe.StripeError:
            pass

    return templates.TemplateResponse("billing.html", {
        "request": request,
        "user": user,
        "subscription": subscription,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


# ---------------------------------------------------------------------------
# Stripe Checkout — upgrade to Pro
# ---------------------------------------------------------------------------

@router.post("/dashboard/billing/checkout")
async def create_checkout_session(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    if not STRIPE_SECRET_KEY or not STRIPE_PRO_PRICE_ID:
        return _redirect("/dashboard/billing?error=Billing+is+not+configured")

    if user.get("plan") == "pro":
        return _redirect("/dashboard/billing?error=You+are+already+on+the+Pro+plan")

    db = await get_database()

    # Reuse or create Stripe customer
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(
            metadata={"pingback_user_id": user["id"]},
        )
        customer_id = customer.id
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE users SET stripe_customer_id = ?, updated_at = ? WHERE id = ?",
            (customer_id, now, user["id"]),
        )
        await db.commit()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": STRIPE_PRO_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/dashboard/billing?success=Welcome+to+Pro!",
        cancel_url=f"{APP_BASE_URL}/dashboard/billing",
        metadata={"pingback_user_id": user["id"]},
    )
    return RedirectResponse(url=session.url, status_code=303)


# ---------------------------------------------------------------------------
# Customer portal — manage/cancel subscription
# ---------------------------------------------------------------------------

@router.post("/dashboard/billing/portal")
async def create_portal_session(request: Request):
    user = await _get_ui_user(request)
    if user is None:
        return _redirect("/login")

    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        return _redirect("/dashboard/billing?error=No+billing+account+found")

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{APP_BASE_URL}/dashboard/billing",
    )
    return RedirectResponse(url=session.url, status_code=303)


# ---------------------------------------------------------------------------
# Stripe webhook handler
# ---------------------------------------------------------------------------

@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    handler = _WEBHOOK_HANDLERS.get(event["type"])
    if handler:
        await handler(event["data"]["object"])

    return {"received": True}


# ---------------------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------------------

async def _handle_subscription_created(sub: dict) -> None:
    await _sync_subscription(sub)


async def _handle_subscription_updated(sub: dict) -> None:
    await _sync_subscription(sub)


async def _handle_subscription_deleted(sub: dict) -> None:
    """Subscription cancelled — downgrade user to free."""
    customer_id = sub.get("customer")
    if not customer_id:
        return
    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE users SET plan = 'free', stripe_subscription_id = NULL, updated_at = ?
           WHERE stripe_customer_id = ?""",
        (now, customer_id),
    )
    await db.commit()
    logger.info("Subscription deleted for customer %s — downgraded to free", customer_id)


async def _handle_payment_failed(invoice: dict) -> None:
    """Log payment failure. Stripe handles dunning/retries automatically."""
    customer_id = invoice.get("customer")
    logger.warning("Payment failed for customer %s (invoice %s)", customer_id, invoice.get("id"))


async def _sync_subscription(sub: dict) -> None:
    """Sync subscription state to the users table."""
    customer_id = sub.get("customer")
    sub_id = sub.get("id")
    status = sub.get("status")
    if not customer_id:
        return

    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()

    if status in ("active", "trialing"):
        await db.execute(
            """UPDATE users SET plan = 'pro', stripe_subscription_id = ?, updated_at = ?
               WHERE stripe_customer_id = ?""",
            (sub_id, now, customer_id),
        )
    elif status in ("canceled", "unpaid", "past_due"):
        await db.execute(
            """UPDATE users SET plan = 'free', stripe_subscription_id = NULL, updated_at = ?
               WHERE stripe_customer_id = ?""",
            (now, customer_id),
        )

    await db.commit()
    logger.info("Synced subscription %s (status=%s) for customer %s", sub_id, status, customer_id)


_WEBHOOK_HANDLERS = {
    "customer.subscription.created": _handle_subscription_created,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_failed": _handle_payment_failed,
}
