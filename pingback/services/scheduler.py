from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pingback.db.connection import get_database
from pingback.db.monitors import find_active_monitors, get_last_check, save_check_result
from pingback.services.checker import check_url

logger = logging.getLogger("pingback.scheduler")

TICK_INTERVAL_SECONDS = 10

_task: asyncio.Task | None = None


async def _tick() -> None:
    db = await get_database()
    monitors = await find_active_monitors(db)
    now = datetime.now(timezone.utc).timestamp()

    for monitor in monitors:
        last_check = await get_last_check(db, monitor.id)
        last_check_time = (
            datetime.fromisoformat(last_check.checked_at).timestamp()
            if last_check
            else 0
        )
        due_at = last_check_time + monitor.interval_seconds

        if now >= due_at:
            try:
                outcome = await check_url(monitor.url)
                await save_check_result(
                    db,
                    monitor.id,
                    outcome.status,
                    outcome.status_code,
                    outcome.response_time_ms,
                    outcome.error,
                )
            except Exception as exc:
                logger.error("Check failed for monitor %s: %s", monitor.id, exc)
                await save_check_result(db, monitor.id, "error", None, None, str(exc))


async def _scheduler_loop() -> None:
    logger.info("Scheduler started (tick every %ds)", TICK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Scheduler tick error")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_scheduler() -> None:
    global _task
    if _task is not None:
        return
    _task = asyncio.create_task(_scheduler_loop())


def stop_scheduler() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
