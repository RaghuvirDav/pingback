"""Plan gating — single source of truth for what each plan allows."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pingback.config import (
    CHECK_INTERVAL_FREE,
    CHECK_INTERVAL_PRO,
    HISTORY_DAYS_FREE,
    HISTORY_DAYS_PRO,
    MAX_MONITORS_BUSINESS,
    MAX_MONITORS_FREE,
    MAX_MONITORS_PRO,
)

logger = logging.getLogger("pingback.plans")


@dataclass(frozen=True)
class PlanLimits:
    max_monitors: Optional[int]   # None means unlimited
    min_interval_seconds: int
    history_days: int


_PLANS: dict[str, PlanLimits] = {
    "free": PlanLimits(
        max_monitors=MAX_MONITORS_FREE,
        min_interval_seconds=CHECK_INTERVAL_FREE,
        history_days=HISTORY_DAYS_FREE,
    ),
    "pro": PlanLimits(
        max_monitors=MAX_MONITORS_PRO,
        min_interval_seconds=CHECK_INTERVAL_PRO,
        history_days=HISTORY_DAYS_PRO,
    ),
    # Business isn't exposed through Stripe Checkout yet; its limits exist so
    # manually-provisioned business customers keep working.
    "business": PlanLimits(
        max_monitors=MAX_MONITORS_BUSINESS,
        min_interval_seconds=CHECK_INTERVAL_PRO,
        history_days=HISTORY_DAYS_PRO,
    ),
}


def limits_for(plan: Optional[str]) -> PlanLimits:
    return _PLANS.get(plan or "free", _PLANS["free"])


class PlanLimitExceeded(Exception):
    """Raised when a request would violate the user's plan limits."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def ensure_monitor_quota(plan: Optional[str], current_count: int) -> None:
    limit = limits_for(plan).max_monitors
    if limit is not None and current_count >= limit:
        raise PlanLimitExceeded(
            f"Monitor limit reached ({limit}). Upgrade to Pro for unlimited monitors."
        )


def ensure_interval_allowed(plan: Optional[str], interval_seconds: int) -> None:
    floor = limits_for(plan).min_interval_seconds
    if interval_seconds < floor:
        minutes = floor // 60
        readable = f"{minutes}-minute" if floor % 60 == 0 else f"{floor}-second"
        raise PlanLimitExceeded(
            f"Your plan requires a minimum {readable} check interval. Upgrade to Pro for 60-second checks."
        )


# ---------------------------------------------------------------------------
# Paddle subscription → local users row
# ---------------------------------------------------------------------------

_ACTIVE_PADDLE_STATUSES = {"active", "trialing"}
_INACTIVE_PADDLE_STATUSES = {"canceled", "paused"}


def _paddle_renews_at(sub: dict) -> Optional[str]:
    """Paddle uses ISO 8601 in `current_billing_period.ends_at`. We store it
    verbatim so downstream code that strips the first 10 chars for display
    keeps working."""
    period = sub.get("current_billing_period") or {}
    ends_at = period.get("ends_at") or sub.get("next_billed_at")
    if not ends_at:
        return None
    try:
        datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ends_at


def _paddle_portal_url(sub: dict) -> Optional[str]:
    urls = sub.get("management_urls") or {}
    return urls.get("update_payment_method") or urls.get("cancel")


async def sync_from_paddle_event(sub: dict) -> None:
    """Apply a Paddle `subscription.{created,updated,canceled}` event to the
    local users row. Matches on `custom_data.user_id` when present (first-seen
    path) and falls back to `paddle_customer_id` for subsequent updates.
    """
    from pingback.db.connection import get_database

    customer_id = sub.get("customer_id")
    sub_id = sub.get("id")
    status = sub.get("status")
    user_id = (sub.get("custom_data") or {}).get("user_id")
    if not customer_id:
        return

    db = await get_database()
    now = datetime.now(timezone.utc).isoformat()
    renews_at = _paddle_renews_at(sub)
    portal_url = _paddle_portal_url(sub)

    if status in _ACTIVE_PADDLE_STATUSES:
        if user_id:
            await db.execute(
                """UPDATE users
                   SET plan = 'pro',
                       paddle_customer_id = ?,
                       paddle_subscription_id = ?,
                       paddle_portal_url = COALESCE(?, paddle_portal_url),
                       plan_renews_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (customer_id, sub_id, portal_url, renews_at, now, user_id),
            )
        else:
            await db.execute(
                """UPDATE users
                   SET plan = 'pro',
                       paddle_subscription_id = ?,
                       paddle_portal_url = COALESCE(?, paddle_portal_url),
                       plan_renews_at = ?,
                       updated_at = ?
                   WHERE paddle_customer_id = ?""",
                (sub_id, portal_url, renews_at, now, customer_id),
            )
    elif status in _INACTIVE_PADDLE_STATUSES:
        # `past_due` intentionally stays on Pro — Paddle is retrying and we'd
        # rather have one noisy retry than silently downgrade a paying user.
        await db.execute(
            """UPDATE users
               SET plan = 'free',
                   paddle_subscription_id = NULL,
                   plan_renews_at = NULL,
                   updated_at = ?
               WHERE paddle_customer_id = ?""",
            (now, customer_id),
        )

    await db.commit()
    logger.info(
        "Synced Paddle subscription %s (status=%s) for customer %s",
        sub_id, status, customer_id,
    )
