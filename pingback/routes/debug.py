"""Debug-only routes for verifying observability wiring.

Only mounted when ``DEBUG_BOOM_ENABLED=1``. Used for one-shot smoke tests
of the Sentry pipeline end-to-end (the route raises; the unhandled
exception handler logs it; Sentry reports it).
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/debug/boom")
async def boom():
    raise RuntimeError("sentry smoke test: /debug/boom")
