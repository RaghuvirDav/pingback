"""Sentry initialization with PII scrubbing.

Called once at process start from ``pingback.main``. If ``SENTRY_DSN`` is
unset the call is a no-op, so local dev and unit tests never hit the network.
"""
from __future__ import annotations

import logging
from typing import Any

from pingback.config import (
    SENTRY_DSN,
    SENTRY_ENVIRONMENT,
    SENTRY_RELEASE,
    SENTRY_TRACES_SAMPLE_RATE,
)
from pingback.logging_config import request_id_var

logger = logging.getLogger("pingback.sentry")

# Header/cookie names we never want to leave the process.
_HEADER_BLOCKLIST = {"authorization", "cookie", "set-cookie", "x-api-key"}


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Strip PII (email, IP, auth headers, cookies) and tag with request_id."""
    user = event.get("user")
    if isinstance(user, dict):
        user.pop("email", None)
        user.pop("ip_address", None)
        user.pop("username", None)

    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in _HEADER_BLOCKLIST:
                    headers.pop(name)
        env = request.get("env")
        if isinstance(env, dict):
            env.pop("REMOTE_ADDR", None)

    rid = request_id_var.get()
    if rid:
        tags = event.setdefault("tags", {})
        if isinstance(tags, dict):
            tags.setdefault("request_id", rid)

    return event


def init_sentry() -> bool:
    """Initialize Sentry if ``SENTRY_DSN`` is set. Returns whether init ran."""
    if not SENTRY_DSN:
        logger.info("sentry_disabled", extra={"reason": "no_dsn"})
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("sentry_sdk_missing")
        return False

    kwargs: dict[str, Any] = {
        "dsn": SENTRY_DSN,
        "traces_sample_rate": SENTRY_TRACES_SAMPLE_RATE,
        "environment": SENTRY_ENVIRONMENT,
        "send_default_pii": False,
        "before_send": _scrub_event,
    }
    if SENTRY_RELEASE:
        kwargs["release"] = SENTRY_RELEASE

    sentry_sdk.init(**kwargs)
    logger.info(
        "sentry_enabled",
        extra={
            "environment": SENTRY_ENVIRONMENT,
            "traces_sample_rate": SENTRY_TRACES_SAMPLE_RATE,
        },
    )
    return True
