"""Scheduler honours per-monitor cadence (MAK-117).

Each monitor stores its own `interval_seconds`, validated at create/edit time
to be >= the owner's plan floor. The scheduler dispatches a check only when
that monitor's own interval has elapsed since the last result — plan changes
do NOT retroactively rewrite stored cadence.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _seed_user_with_monitor(
    db_path: str,
    plan: str,
    monitor_interval_seconds: int,
    last_check_age_seconds: int,
) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    monitor_id = str(uuid.uuid4())
    check_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    last_check_at = (now - timedelta(seconds=last_check_age_seconds)).isoformat()
    now_iso = now.isoformat()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO users (id, email, name, plan, created_at, updated_at, email_verified) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (user_id, f"sched-{user_id[:8]}@x", "u", plan, now_iso, now_iso),
        )
        con.execute(
            "INSERT INTO monitors (id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at) "
            "VALUES (?, ?, 'm', 'https://example.com', ?, 'active', 0, ?, ?)",
            (monitor_id, user_id, monitor_interval_seconds, now_iso, now_iso),
        )
        con.execute(
            "INSERT INTO check_results (id, monitor_id, status, status_code, response_time_ms, error, checked_at) "
            "VALUES (?, ?, 'up', 200, 10, NULL, ?)",
            (check_id, monitor_id, last_check_at),
        )
        con.commit()
    return user_id, monitor_id


def _check_count(db_path: str, monitor_id: str) -> int:
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM check_results WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()
    return row[0]


async def _init_schema():
    from pingback.db.connection import get_database
    await get_database()


@pytest.mark.asyncio
async def test_scheduler_skips_when_monitor_interval_not_elapsed(app_ctx):
    """Pro user with a 600s monitor — only 120s old. Not due."""
    await _init_schema()
    db_path = os.environ["DB_PATH"]
    _, monitor_id = _seed_user_with_monitor(
        db_path, plan="pro", monitor_interval_seconds=600, last_check_age_seconds=120,
    )

    from pingback.services import scheduler

    fake = AsyncMock()
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    fake.assert_not_called()
    assert _check_count(db_path, monitor_id) == 1


@pytest.mark.asyncio
async def test_scheduler_runs_when_monitor_interval_elapsed(app_ctx):
    """Same Pro user, but the 600s monitor is 700s stale — due."""
    await _init_schema()
    db_path = os.environ["DB_PATH"]
    _, monitor_id = _seed_user_with_monitor(
        db_path, plan="pro", monitor_interval_seconds=600, last_check_age_seconds=700,
    )

    from pingback.services import checker, scheduler

    fake = AsyncMock(return_value=checker.CheckOutcome(
        status="up", status_code=200, response_time_ms=12, error=None,
    ))
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    fake.assert_called_once()
    assert _check_count(db_path, monitor_id) == 2


@pytest.mark.asyncio
async def test_scheduler_uses_monitor_interval_not_plan_floor(app_ctx):
    """Pro plan floor is 60s but the monitor was created at 1800s. Two minutes
    after the last check the monitor is NOT due, even though the plan floor
    technically allows polling every minute. Plan floor is a *minimum*, not a
    cadence — scheduler must trust the per-monitor value."""
    await _init_schema()
    db_path = os.environ["DB_PATH"]
    _, monitor_id = _seed_user_with_monitor(
        db_path, plan="pro", monitor_interval_seconds=1800, last_check_age_seconds=120,
    )

    from pingback.services import scheduler

    fake = AsyncMock()
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    fake.assert_not_called()
    assert _check_count(db_path, monitor_id) == 1
