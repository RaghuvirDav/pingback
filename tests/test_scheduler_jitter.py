"""Per-monitor scheduler jitter (MAK-148).

The naive scheduler fired every monitor whose interval had elapsed at the
exact tick boundary, which clobbers a single uvicorn worker once paid plans
load up the system (capacity write-up in MAK-144). Each monitor now gets a
deterministic offset in [-5, +4] seconds, so concurrent fires at any second
are bounded by ⌈total_monitors / 10⌉ while drift stays within ±5s of the
declared cadence.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def test_jitter_offset_is_in_range_and_stable():
    from pingback.services.scheduler import _jitter_offset_seconds

    sample_id = "00000000-0000-0000-0000-000000000001"
    first = _jitter_offset_seconds(sample_id)
    assert -5 <= first <= 4
    assert _jitter_offset_seconds(sample_id) == first
    # 500 random ids should cover every offset bucket — distribution is
    # near-uniform but the per-bucket count is not exact.
    offsets = {_jitter_offset_seconds(str(uuid.uuid4())) for _ in range(500)}
    assert offsets == set(range(-5, 5))


def test_jitter_keeps_business_floor_within_five_seconds():
    """A 30s BUSINESS-floor monitor must never drift outside [25s, 35s] gaps."""
    from pingback.services.scheduler import _jitter_offset_seconds

    for _ in range(1000):
        offset = _jitter_offset_seconds(str(uuid.uuid4()))
        gap = 30 + offset
        assert 25 <= gap <= 34


def _seed_user(con: sqlite3.Connection, plan: str = "pro") -> str:
    user_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO users (id, email, name, plan, created_at, updated_at, email_verified) "
        "VALUES (?, ?, 'u', ?, ?, ?, 1)",
        (user_id, f"jitter-{user_id[:8]}@x", plan, now_iso, now_iso),
    )
    return user_id


def _seed_monitor(
    con: sqlite3.Connection,
    user_id: str,
    monitor_id: str,
    *,
    interval: int,
    url: str,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO monitors (id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at) "
        "VALUES (?, ?, 'm', ?, ?, 'active', 0, ?, ?)",
        (monitor_id, user_id, url, interval, now_iso, now_iso),
    )


def _seed_check(con: sqlite3.Connection, monitor_id: str, *, age_seconds: int) -> None:
    checked_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    con.execute(
        "INSERT INTO check_results (id, monitor_id, status, status_code, response_time_ms, error, checked_at) "
        "VALUES (?, ?, 'up', 200, 10, NULL, ?)",
        (str(uuid.uuid4()), monitor_id, checked_at),
    )


def _check_count(db_path: str, monitor_id: str) -> int:
    with sqlite3.connect(db_path) as con:
        return con.execute(
            "SELECT COUNT(*) FROM check_results WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]


def _id_with_offset(target: int) -> str:
    """Return a monitor id whose jitter offset equals `target`."""
    from pingback.services.scheduler import _jitter_offset_seconds

    for _ in range(2000):
        candidate = str(uuid.uuid4())
        if _jitter_offset_seconds(candidate) == target:
            return candidate
    raise AssertionError(f"no id found with jitter offset {target}")


@pytest.mark.asyncio
async def test_negative_jitter_fires_a_few_seconds_early(app_ctx):
    """A monitor with offset=-5 fires at age=56 on a 60s cadence (would
    otherwise have to wait ~4s longer)."""
    from pingback.db.connection import get_database

    await get_database()
    db_path = os.environ["DB_PATH"]
    early_id = _id_with_offset(-5)

    with sqlite3.connect(db_path) as con:
        user_id = _seed_user(con)
        _seed_monitor(con, user_id, early_id, interval=60, url="https://example.com")
        _seed_check(con, early_id, age_seconds=56)
        con.commit()

    from pingback.services import checker, scheduler

    fake = AsyncMock(return_value=checker.CheckOutcome(
        status="up", status_code=200, response_time_ms=12, error=None,
    ))
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    fake.assert_called_once()
    assert _check_count(db_path, early_id) == 2


@pytest.mark.asyncio
async def test_positive_jitter_holds_back_until_window_closes(app_ctx):
    """A monitor with offset=+4 must NOT fire at age=61 on a 60s cadence —
    the naive scheduler would have fired it a second early."""
    from pingback.db.connection import get_database

    await get_database()
    db_path = os.environ["DB_PATH"]
    late_id = _id_with_offset(4)

    with sqlite3.connect(db_path) as con:
        user_id = _seed_user(con)
        _seed_monitor(con, user_id, late_id, interval=60, url="https://example.com")
        _seed_check(con, late_id, age_seconds=61)
        con.commit()

    from pingback.services import scheduler

    fake = AsyncMock()
    with patch.object(scheduler, "check_url", fake):
        await scheduler._tick()

    fake.assert_not_called()
    assert _check_count(db_path, late_id) == 1


@pytest.mark.asyncio
async def test_herd_of_200_monitors_spreads_across_window(app_ctx):
    """200 monitors on a shared 60s cadence must not all fire on the same
    second. With balanced offsets every second within the 10s jitter window
    sees at most ⌈200/10⌉ = 20 fires, and every monitor fires exactly once
    across the window."""
    from pingback.services.scheduler import _jitter_offset_seconds

    # Build 200 ids with exactly 20 per offset bucket so the per-second
    # bound holds deterministically. (Real-world distribution is near-
    # uniform but not exact — `test_jitter_offset_is_in_range_and_stable`
    # covers the unconstrained spread separately.)
    bucket_size = 20
    buckets: dict[int, list[str]] = {off: [] for off in range(-5, 5)}
    monitor_ids: list[str] = []
    while len(monitor_ids) < 200:
        candidate = str(uuid.uuid4())
        off = _jitter_offset_seconds(candidate)
        if len(buckets[off]) < bucket_size:
            buckets[off].append(candidate)
            monitor_ids.append(candidate)

    from pingback.db.connection import get_database

    await get_database()
    db_path = os.environ["DB_PATH"]

    # last_check_age = 55s on a 60s interval means a monitor with offset OFF
    # becomes due at synthetic second `5 + OFF` (so OFF=-5 fires at sec 0,
    # OFF=+4 fires at sec 9). 10 evenly-populated buckets → 20 fires/sec.
    base_now = datetime.now(timezone.utc).replace(microsecond=0)
    last_check_iso = (base_now - timedelta(seconds=55)).isoformat()
    with sqlite3.connect(db_path) as con:
        user_id = _seed_user(con)
        for mid in monitor_ids:
            _seed_monitor(
                con, user_id, mid, interval=60, url=f"https://example.com/{mid}",
            )
            con.execute(
                "INSERT INTO check_results (id, monitor_id, status, status_code, response_time_ms, error, checked_at) "
                "VALUES (?, ?, 'up', 200, 10, NULL, ?)",
                (str(uuid.uuid4()), mid, last_check_iso),
            )
        con.commit()

    from pingback.services import checker, scheduler

    real_datetime = datetime
    fire_log: list[tuple[str, int]] = []

    class _ClockProxy:
        _now = base_now

        @staticmethod
        def now(tz=None):
            return _ClockProxy._now

        @staticmethod
        def fromisoformat(s):
            return real_datetime.fromisoformat(s)

    async def fake_check(url):
        monitor_id = url.rsplit("/", 1)[-1]
        sec = int((_ClockProxy._now - base_now).total_seconds())
        fire_log.append((monitor_id, sec))
        return checker.CheckOutcome(
            status="up", status_code=200, response_time_ms=12, error=None,
        )

    with patch.object(scheduler, "check_url", side_effect=fake_check), \
         patch.object(scheduler, "datetime", _ClockProxy):
        for sec in range(10):
            _ClockProxy._now = base_now + timedelta(seconds=sec)
            await scheduler._tick()

    fired_ids = [mid for mid, _ in fire_log]
    assert sorted(fired_ids) == sorted(monitor_ids), (
        "expected every monitor to fire exactly once; "
        f"missing={set(monitor_ids) - set(fired_ids)} "
        f"extra={[mid for mid in fired_ids if fired_ids.count(mid) > 1][:5]}"
    )

    per_second = Counter(sec for _, sec in fire_log)
    assert max(per_second.values()) <= 20, (
        f"herd not spread — per-second fires: {sorted(per_second.items())}"
    )
    assert len(per_second) == 10, (
        f"expected 10 distinct fire seconds, got {sorted(per_second.items())}"
    )
