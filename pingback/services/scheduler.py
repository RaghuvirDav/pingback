from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pingback.config import ABANDONED_ACCOUNT_DAYS, RETENTION_DAYS
from pingback.db.connection import get_database
from pingback.db.monitors import (
    archive_abandoned_free_accounts,
    find_active_monitors,
    get_last_check,
    purge_expired_check_results,
    save_check_result,
)
from pingback.db.rollups import compact_recent
from pingback.services.checker import check_url
from pingback.services.email import send_daily_digests

logger = logging.getLogger("pingback.scheduler")

# Tick fires at this granularity; a monitor is dispatched when its own
# `interval_seconds` has elapsed since the last check. 10s gives <=10s drift
# on the 30s BUSINESS floor, which is acceptable for uptime monitoring.
TICK_INTERVAL_SECONDS = 10
_PURGE_INTERVAL_SECONDS = 86400  # Run retention purge once per day
# MAK-126: digest evaluation runs every 15 minutes. The eligibility filter
# uses a ±7-minute window around 08:00 local, so each user is matched by
# at most one tick per day. Worst-case delivery latency: ~15 minutes.
_DIGEST_TICK_SECONDS = 15 * 60
# MAK-147: rollup compaction runs once per minute. The compactor itself decides
# whether the just-elapsed 1m / 5m / 1h windows actually need writing, so this
# is just the *evaluation* cadence.
_ROLLUP_TICK_SECONDS = 60

_task: asyncio.Task | None = None
_last_purge_time: float = 0
_last_digest_tick_at: float = 0
_last_rollup_tick_at: float = 0


async def _run_check(db, monitor) -> None:
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


async def _tick() -> None:
    db = await get_database()
    monitors = await find_active_monitors(db)
    now = datetime.now(timezone.utc).timestamp()

    due: list = []
    for monitor in monitors:
        last_check = await get_last_check(db, monitor.id)
        last_check_time = (
            datetime.fromisoformat(last_check.checked_at).timestamp()
            if last_check
            else 0
        )
        if now >= last_check_time + monitor.interval_seconds:
            due.append(monitor)

    if due:
        # Fan out concurrently so one slow target can't starve the rest. A
        # Business customer at the 30s floor × 100 monitors is ~3.3 checks/sec;
        # httpx async with the 30s checker timeout absorbs that on one worker.
        await asyncio.gather(*(_run_check(db, m) for m in due), return_exceptions=True)


async def _maybe_purge() -> None:
    """Run the data-retention purge and abandoned-account cleanup once per day."""
    global _last_purge_time
    now = datetime.now(timezone.utc).timestamp()
    if now - _last_purge_time < _PURGE_INTERVAL_SECONDS:
        return
    _last_purge_time = now
    db = await get_database()
    try:
        await purge_expired_check_results(db, RETENTION_DAYS)
    except Exception:
        logger.exception("Retention purge error")
    try:
        await archive_abandoned_free_accounts(db, ABANDONED_ACCOUNT_DAYS)
    except Exception:
        logger.exception("Abandoned-account cleanup error")


async def _maybe_send_digests() -> None:
    """Evaluate digest delivery on a 15-minute cadence (MAK-126).

    The per-user filter inside `send_daily_digests` matches users whose local
    time is within ±7 minutes of 08:00 — pairing that with a 15-minute tick
    keeps worst-case delivery latency at ~15 minutes (down from ~60 with the
    previous hourly tick) without paying the DB scan every 10 seconds.
    """
    global _last_digest_tick_at
    now_utc = datetime.now(timezone.utc)
    now_ts = now_utc.timestamp()
    if now_ts - _last_digest_tick_at < _DIGEST_TICK_SECONDS:
        return
    _last_digest_tick_at = now_ts
    try:
        await send_daily_digests(now_utc)
    except Exception:
        logger.exception("Daily digest send error")


async def _maybe_compact_rollups() -> None:
    """Roll up the most recently completed 1m/5m/1h windows once per minute."""
    global _last_rollup_tick_at
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts - _last_rollup_tick_at < _ROLLUP_TICK_SECONDS:
        return
    _last_rollup_tick_at = now_ts
    try:
        db = await get_database()
        await compact_recent(db)
    except Exception:
        logger.exception("Rollup compaction error")


async def _scheduler_loop() -> None:
    logger.info("Scheduler started (tick every %ds)", TICK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Scheduler tick error")
        try:
            await _maybe_purge()
        except Exception:
            logger.exception("Purge tick error")
        try:
            await _maybe_send_digests()
        except Exception:
            logger.exception("Digest tick error")
        try:
            await _maybe_compact_rollups()
        except Exception:
            logger.exception("Rollup tick error")
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
