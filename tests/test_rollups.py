"""MAK-147 — rollup tables for check_results.

Direct DB tests (no HTTP layer): the rollup module is pure data manipulation
and easier to verify against synthetic raw rows.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from pingback.db.rollups import (
    backfill,
    compact_recent,
    compact_window,
    count_user_checks_in_window,
    floor_to_bucket,
    get_monitor_window_stats,
    pick_tier,
    reset_compaction_state,
)
from pingback.db.schema import initialize_database


@pytest.fixture(autouse=True)
def _reset_compaction_state():
    """Module-level memoisation in `rollups` would otherwise leak between tests."""
    reset_compaction_state()
    yield
    reset_compaction_state()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "rollups.db"


async def _open_db(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await initialize_database(db)
    return db


async def _seed_user_and_monitor(db, *, plan: str = "pro") -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    monitor_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO users (id, email, plan) VALUES (?, ?, ?)",
        (user_id, f"u-{user_id[:8]}@example.com", plan),
    )
    await db.execute(
        """INSERT INTO monitors (id, user_id, name, url, interval_seconds, status)
           VALUES (?, ?, 'M', 'https://example.com', 60, 'active')""",
        (monitor_id, user_id),
    )
    await db.commit()
    return user_id, monitor_id


async def _seed_check(db, monitor_id, *, status, latency, when: datetime):
    await db.execute(
        """INSERT INTO check_results
            (id, monitor_id, status, status_code, response_time_ms, error, checked_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?)""",
        (str(uuid.uuid4()), monitor_id, status, 200 if status == "up" else 500, latency, when.isoformat()),
    )


def _run(coro):
    """Tiny helper so tests stay sync-looking. pytest-asyncio not in use here."""
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() is False else asyncio.run(coro)


# ---------------------------------------------------------------------------
# pick_tier
# ---------------------------------------------------------------------------


def test_pick_tier_boundaries():
    # <1h → raw
    assert pick_tier(0) == "raw"
    assert pick_tier(60) == "raw"
    assert pick_tier(3599) == "raw"
    # 1h..<24h → 1m
    assert pick_tier(3600) == "1m"
    assert pick_tier(86399) == "1m"
    # 24h..<7d → 5m
    assert pick_tier(86400) == "5m"
    assert pick_tier(7 * 86400 - 1) == "5m"
    # >=7d → 1h
    assert pick_tier(7 * 86400) == "1h"
    assert pick_tier(365 * 86400) == "1h"


# ---------------------------------------------------------------------------
# floor_to_bucket — bucket edges must round DOWN, not nearest
# ---------------------------------------------------------------------------


def test_floor_to_bucket_minute_edges():
    # exactly on the minute → unchanged
    t = datetime(2026, 1, 1, 12, 34, 0, tzinfo=timezone.utc)
    assert floor_to_bucket(t, 60) == t
    # 59s past → still in the same bucket
    assert floor_to_bucket(t.replace(second=59), 60) == t
    # microseconds dropped
    assert floor_to_bucket(t.replace(second=30, microsecond=999_999), 60) == t


def test_floor_to_bucket_naive_datetime_is_utc():
    naive = datetime(2026, 1, 1, 12, 34, 30)  # tzinfo=None
    floored = floor_to_bucket(naive, 60)
    assert floored == datetime(2026, 1, 1, 12, 34, 0, tzinfo=timezone.utc)


def test_floor_to_bucket_5m_and_1h():
    t = datetime(2026, 1, 1, 12, 37, 42, tzinfo=timezone.utc)
    assert floor_to_bucket(t, 300) == datetime(2026, 1, 1, 12, 35, 0, tzinfo=timezone.utc)
    assert floor_to_bucket(t, 3600) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# compact_window — rollup math
# ---------------------------------------------------------------------------


def test_compact_window_aggregates_correctly(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            window_start = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
            # 4 checks in the same minute: 3 up, 1 down. Latencies 100/200/300/400.
            await _seed_check(db, monitor_id, status="up",   latency=100, when=window_start)
            await _seed_check(db, monitor_id, status="up",   latency=200, when=window_start + timedelta(seconds=15))
            await _seed_check(db, monitor_id, status="up",   latency=300, when=window_start + timedelta(seconds=30))
            await _seed_check(db, monitor_id, status="down", latency=400, when=window_start + timedelta(seconds=45))
            await db.commit()

            written = await compact_window(db, "1m", window_start)
            assert written == 1

            row = await (await db.execute(
                "SELECT * FROM check_results_1m WHERE monitor_id = ?", (monitor_id,)
            )).fetchone()
            assert row["check_count"] == 4
            assert row["ok_count"] == 3
            assert row["fail_count"] == 1
            assert row["avg_latency_ms"] == pytest.approx(250.0)
            # nearest-rank: p50 of 4 sorted = idx ceil(0.5*4)-1 = 1 → 200
            assert row["p50_latency_ms"] == 200
            assert row["p95_latency_ms"] == 400
            assert row["p99_latency_ms"] == 400
        finally:
            await db.close()

    asyncio.run(run())


def test_compact_window_excludes_rows_outside(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            window_start = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
            # one row inside, two rows just outside the window on either side
            await _seed_check(db, monitor_id, status="up", latency=100, when=window_start - timedelta(seconds=1))
            await _seed_check(db, monitor_id, status="up", latency=200, when=window_start)
            await _seed_check(db, monitor_id, status="up", latency=300, when=window_start + timedelta(seconds=60))
            await db.commit()

            await compact_window(db, "1m", window_start)
            row = await (await db.execute(
                "SELECT check_count FROM check_results_1m WHERE monitor_id = ?", (monitor_id,)
            )).fetchone()
            assert row["check_count"] == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_compact_window_idempotent(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            window_start = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
            await _seed_check(db, monitor_id, status="up", latency=100, when=window_start)
            await db.commit()

            await compact_window(db, "1m", window_start)
            await compact_window(db, "1m", window_start)  # second pass replaces, not appends
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM check_results_1m WHERE monitor_id = ?", (monitor_id,)
            )).fetchone()
            assert count["n"] == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_compact_window_no_rows_means_no_rollup(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            window_start = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
            written = await compact_window(db, "1m", window_start)
            assert written == 0
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM check_results_1m"
            )).fetchone()
            assert count["n"] == 0
        finally:
            await db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# backfill across many windows
# ---------------------------------------------------------------------------


def test_backfill_writes_one_row_per_active_window(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            t0 = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
            # one check per minute for 10 minutes
            for i in range(10):
                await _seed_check(db, monitor_id, status="up", latency=100, when=t0 + timedelta(minutes=i, seconds=5))
            await db.commit()

            n = await backfill(db, "1m", t0, t0 + timedelta(minutes=10))
            assert n == 10

            # rerun is idempotent
            n2 = await backfill(db, "1m", t0, t0 + timedelta(minutes=10))
            assert n2 == 10
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM check_results_1m WHERE monitor_id = ?", (monitor_id,)
            )).fetchone()
            assert count["n"] == 10
        finally:
            await db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Read tier dispatch — get_monitor_window_stats
# ---------------------------------------------------------------------------


def test_get_monitor_window_stats_uses_raw_for_short_window(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 1, 12, 30, 0, tzinfo=timezone.utc)
            # Three checks in the last 30 minutes
            for i in range(3):
                await _seed_check(db, monitor_id, status="up", latency=150, when=now - timedelta(minutes=i + 1))
            await db.commit()

            stats = await get_monitor_window_stats(db, monitor_id, 1800, now=now)
            assert stats["tier"] == "raw"
            assert stats["check_count"] == 3
            assert stats["uptime_pct"] == 100.0
            assert stats["avg_latency_ms"] == pytest.approx(150.0)
        finally:
            await db.close()

    asyncio.run(run())


def test_get_monitor_window_stats_uses_5m_for_3day_window(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
            # Build a 5m rollup row 1 day back: 60 ok, 0 fail
            window = floor_to_bucket(now - timedelta(days=1), 300)
            await db.execute(
                """INSERT INTO check_results_5m
                    (monitor_id, window_start, check_count, ok_count, fail_count,
                     avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms)
                    VALUES (?, ?, 60, 60, 0, 100.0, 100, 100, 100)""",
                (monitor_id, window.isoformat()),
            )
            # And another rollup row outside the 3-day window — must be ignored
            old_window = floor_to_bucket(now - timedelta(days=10), 300)
            await db.execute(
                """INSERT INTO check_results_5m
                    (monitor_id, window_start, check_count, ok_count, fail_count,
                     avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms)
                    VALUES (?, ?, 60, 0, 60, 999.0, 999, 999, 999)""",
                (monitor_id, old_window.isoformat()),
            )
            await db.commit()

            stats = await get_monitor_window_stats(db, monitor_id, 3 * 86400, now=now)
            assert stats["tier"] == "5m"
            assert stats["check_count"] == 60
            assert stats["uptime_pct"] == 100.0
        finally:
            await db.close()

    asyncio.run(run())


def test_get_monitor_window_stats_uses_1h_for_30day_window(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
            stats = await get_monitor_window_stats(db, monitor_id, 30 * 86400, now=now)
            # Per spec: >7d → 1h tier
            assert stats["tier"] == "1h"
        finally:
            await db.close()

    asyncio.run(run())


def test_get_monitor_window_stats_uses_1h_for_year_window(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
            stats = await get_monitor_window_stats(db, monitor_id, 365 * 86400, now=now)
            assert stats["tier"] == "1h"
        finally:
            await db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# count_user_checks_in_window — used by dashboard 24h tile
# ---------------------------------------------------------------------------


def test_count_user_checks_24h_via_5m_rollup(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            user_id, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
            # 24h window dispatches to the 5m tier (per pick_tier boundaries).
            # Two 5m rollup rows inside the 24h window: 5 + 7 = 12 checks
            for offset, n in ((1, 5), (2, 7)):
                w = floor_to_bucket(now - timedelta(hours=offset), 300)
                await db.execute(
                    """INSERT INTO check_results_5m
                        (monitor_id, window_start, check_count, ok_count, fail_count,
                         avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms)
                        VALUES (?, ?, ?, ?, 0, 100.0, 100, 100, 100)""",
                    (monitor_id, w.isoformat(), n, n),
                )
            await db.commit()

            n = await count_user_checks_in_window(db, user_id, 24 * 3600, now=now)
            assert n == 12
        finally:
            await db.close()

    asyncio.run(run())


def test_count_user_checks_under_24h_via_1m_rollup(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            user_id, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
            # 12-hour window dispatches to the 1m tier.
            for offset, n in ((1, 5), (2, 7)):
                w = floor_to_bucket(now - timedelta(hours=offset), 60)
                await db.execute(
                    """INSERT INTO check_results_1m
                        (monitor_id, window_start, check_count, ok_count, fail_count,
                         avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms)
                        VALUES (?, ?, ?, ?, 0, 100.0, 100, 100, 100)""",
                    (monitor_id, w.isoformat(), n, n),
                )
            await db.commit()

            n = await count_user_checks_in_window(db, user_id, 12 * 3600, now=now)
            assert n == 12
        finally:
            await db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# compact_recent — only fires 5m / 1h on the right boundary
# ---------------------------------------------------------------------------


def test_compact_recent_skips_already_compacted_buckets(db_path):
    """A second call without a new bucket boundary must be a no-op."""
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 1, 12, 3, 30, tzinfo=timezone.utc)
            for i in range(120):
                await _seed_check(db, monitor_id, status="up", latency=100, when=now - timedelta(seconds=i + 1))
            await db.commit()

            first = await compact_recent(db, now=now)
            assert "1m" in first

            # Second call at the same minute: 1m bucket unchanged → skipped.
            second = await compact_recent(db, now=now)
            assert "1m" not in second
        finally:
            await db.close()

    asyncio.run(run())


def test_compact_recent_writes_5m_bucket_when_data_exists(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 1, 12, 5, 0, tzinfo=timezone.utc)  # on a 5m boundary
            # Check inside the 12:00–12:05 bucket so it has data to roll up.
            await _seed_check(db, monitor_id, status="up", latency=120, when=now - timedelta(seconds=30))
            await db.commit()

            summary = await compact_recent(db, now=now)
            # The 12:00–12:05 5m bucket is fully elapsed and has the seeded check.
            assert summary.get("5m") == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_compact_recent_writes_1h_bucket_at_top_of_hour(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 1, 13, 0, 0, tzinfo=timezone.utc)
            await _seed_check(db, monitor_id, status="up", latency=120, when=now - timedelta(minutes=30))
            await db.commit()

            summary = await compact_recent(db, now=now)
            assert summary.get("1h") == 1
        finally:
            await db.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 30-day stats via rollups should match raw math (uses get_monitor_window_stats
# with an explicit `now` so the test isn't wall-clock dependent)
# ---------------------------------------------------------------------------


def test_30day_stats_via_1h_rollups_match_raw_math(db_path):
    async def run():
        db = await _open_db(db_path)
        try:
            _, monitor_id = await _seed_user_and_monitor(db)
            now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
            # 80 up + 20 down, one per hour, all inside the last ~5 days.
            # Each lands in its own 1h bucket → check_count adds up cleanly.
            for i in range(80):
                await _seed_check(db, monitor_id, status="up", latency=100, when=now - timedelta(hours=i + 1))
            for i in range(20):
                await _seed_check(db, monitor_id, status="down", latency=200, when=now - timedelta(hours=80 + i + 1))
            await db.commit()

            # 30-day window dispatches to the 1h tier per spec.
            await backfill(db, "1h", now - timedelta(days=10), now)

            stats = await get_monitor_window_stats(db, monitor_id, 30 * 86400, now=now)
            assert stats["tier"] == "1h"
            assert stats["check_count"] == 100
            assert stats["uptime_pct"] == pytest.approx(80.0)
        finally:
            await db.close()

    asyncio.run(run())
