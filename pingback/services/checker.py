from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import httpx

from pingback.config import CHECK_TIMEOUT_SECONDS
from pingback.models import CheckStatus


@dataclass
class CheckOutcome:
    status: CheckStatus
    status_code: Optional[int]
    response_time_ms: Optional[int]
    error: Optional[str]


async def check_url(url: str) -> CheckOutcome:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=CHECK_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Pingback/0.1.0"},
        ) as client:
            response = await client.get(url)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if response.is_success:
            return CheckOutcome(
                status="up",
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                error=None,
            )
        return CheckOutcome(
            status="down",
            status_code=response.status_code,
            response_time_ms=elapsed_ms,
            error=f"HTTP {response.status_code} {response.reason_phrase}",
        )
    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return CheckOutcome(
            status="down",
            status_code=None,
            response_time_ms=elapsed_ms,
            error=f"Timeout after {CHECK_TIMEOUT_SECONDS}s",
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return CheckOutcome(
            status="error",
            status_code=None,
            response_time_ms=elapsed_ms,
            error=str(exc),
        )
