"""Plan gating — single source of truth for what each plan allows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pingback.config import (
    CHECK_INTERVAL_FREE,
    CHECK_INTERVAL_PRO,
    HISTORY_DAYS_BUSINESS,
    HISTORY_DAYS_FREE,
    HISTORY_DAYS_PRO,
    MAX_MONITORS_BUSINESS,
    MAX_MONITORS_FREE,
    MAX_MONITORS_PRO,
)


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
    # Business is contact-sales only; limits exist so manually-provisioned
    # Business customers see the right caps + 1-year retention.
    "business": PlanLimits(
        max_monitors=MAX_MONITORS_BUSINESS,
        min_interval_seconds=CHECK_INTERVAL_PRO,
        history_days=HISTORY_DAYS_BUSINESS,
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
            f"Monitor limit reached ({limit}). Upgrade to Pro for more monitors."
        )


def ensure_interval_allowed(plan: Optional[str], interval_seconds: int) -> None:
    floor = limits_for(plan).min_interval_seconds
    if interval_seconds < floor:
        minutes = floor // 60
        readable = f"{minutes}-minute" if floor % 60 == 0 else f"{floor}-second"
        raise PlanLimitExceeded(
            f"Your plan requires a minimum {readable} check interval. Upgrade to Pro for 60-second checks."
        )
