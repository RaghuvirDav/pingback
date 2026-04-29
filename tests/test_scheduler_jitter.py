"""Per-monitor jitter offset (MAK-169).

Same-interval monitors created in one bulk operation share their effective
"due" instant on the first tick after a restart, which fan-outs the whole
cohort onto a single tick and concentrates outbound load. The jitter offset
is a deterministic, bounded-by-10%-of-interval phase shift derived from the
monitor id so the same monitor always lands on the same offset.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _seed(db_path: str, monitor_id: str, interval: int, age_seconds: float) -> str:
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    last_check_at = (now - timedelta(seconds=age_seconds)).isoformat()
    now_iso = now.isoformat()
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO users (id, email, name, plan, created_at, updated_at, email_verified) "
            "VALUES (?, ?, 'u', 'pro', ?, ?, 1)",
            (user_id, f"j-{user_id[:8]}@x", now_iso, now_iso),
        )
        con.execute(
            "INSERT INTO monitors (id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at) "
            "VALUES (?, ?, 'm', 'https://example.com', ?, 'active', 0, ?, ?)",
            (monitor_id, user_id, interval, now_iso, now_iso),
        )
        con.execute(
            "INSERT INTO check_results (id, monitor_id, status, status_code, response_time_ms, error, checked_at) "
            "VALUES (?, ?, 'up', 200, 10, NULL, ?)",
            (str(uuid.uuid4()), monitor_id, last_check_at),
        )
        con.commit()
    return monitor_id


def test_phase_offset_is_bounded_and_deterministic():
    from pingback.services.scheduler import _phase_offset_seconds

    # Bounded: 0 <= offset < interval * 0.1
    for interval in (30, 60, 300):
        for _ in range(50):
            offset = _phase_offset_seconds(str(uuid.uuid4()), interval)
            assert 0.0 <= offset < interval * 0.1

    # Deterministic: same id → same offset.
    mid = "deadbeef-1234-1234-1234-deadbeefcafe"
    assert _phase_offset_seconds(mid, 60) == _phase_offset_seconds(mid, 60)


def test_phase_offset_deconcentrates_same_interval_cohort():
    from pingback.services.scheduler import _phase_offset_seconds

    # 200 distinct ids on the same 60s interval should populate the [0, 6) range
    # broadly — i.e. they don't all collapse to the same offset.
    offsets = {round(_phase_offset_seconds(str(uuid.uuid4()), 60), 2) for _ in range(200)}
    assert len(offsets) > 50, "jitter is not distributing across monitors"


@pytest.mark.asyncio
async def test_scheduler_skips_monitor_inside_jitter_window(app_ctx):
    """Monitor age sits inside the jitter window (interval + jitter). Not due."""
    from pingback.db.connection import get_database
    from pingback.services import scheduler

    await get_database()
    db_path = os.environ["DB_PATH"]

    # Pick a monitor id whose offset is large enough to cover the test slack.
    # 60s interval → max jitter 6s. Age = 60.5s should be jitter-gated for any
    # monitor whose offset > 0.5s, which a deterministic SHA1 prefix easily
    # satisfies for at least one of these candidates.
    candidate_ids = [str(uuid.uuid4()) for _ in range(20)]
    chosen = next(
        m for m in candidate_ids
        if scheduler._phase_offset_seconds(m, 60) > 1.0
    )
    _seed(db_path, chosen, interval=60, age_seconds=60.5)

    fake = AsyncMock()
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    # Inside the jitter window → not yet due.
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_runs_monitor_past_jitter_window(app_ctx):
    """Monitor age exceeds interval + max jitter. Always due."""
    from pingback.db.connection import get_database
    from pingback.services import checker, scheduler

    await get_database()
    db_path = os.environ["DB_PATH"]

    monitor_id = str(uuid.uuid4())
    # 60s interval, max jitter 6s → age 80s clears with margin for any offset.
    _seed(db_path, monitor_id, interval=60, age_seconds=80)

    fake = AsyncMock(return_value=checker.CheckOutcome(
        status="up", status_code=200, response_time_ms=12, error=None,
    ))
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    fake.assert_called_once()
