"""Rollup tables for check_results (MAK-147).

Reads against raw `check_results` scan O(checks) rows per query, which is fatal
at BUSINESS retention (1yr × 30s = ~1M rows per monitor). We materialise
1-minute, 5-minute, and 1-hour aggregates so dashboard windows scan ≤10k rows.

Compaction always reads from raw to keep percentiles honest — you can't average
percentiles. The cost is bounded: a 1h window holds at most ~120 raw rows
(30s floor), so worst-case the scheduler scans ~120 rows/monitor/hour.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

import aiosqlite

logger = logging.getLogger("pingback.db.rollups")

Tier = Literal["raw", "1m", "5m", "1h"]

_TIER_TABLE = {
    "1m": "check_results_1m",
    "5m": "check_results_5m",
    "1h": "check_results_1h",
}

_TIER_BUCKET_SECONDS = {
    "1m": 60,
    "5m": 300,
    "1h": 3600,
}


def floor_to_bucket(dt: datetime, bucket_seconds: int) -> datetime:
    """Floor `dt` to the start of its bucket. Always UTC, second-precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    epoch = int(dt.timestamp())
    floored = (epoch // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc).replace(microsecond=0)


def pick_tier(window_seconds: float) -> Tier:
    """Choose the cheapest tier that still covers the requested window.

    Boundaries match the MAK-147 spec:
      <1h           -> raw   (need precision; window is small anyway)
      1h .. <24h    -> 1m
      24h .. <7d    -> 5m
      >=7d          -> 1h
    """
    if window_seconds < 3600:
        return "raw"
    if window_seconds < 86400:
        return "1m"
    if window_seconds < 7 * 86400:
        return "5m"
    return "1h"


def _percentile(sorted_values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile on an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank: index = ceil(pct/100 * N) - 1, clamped.
    rank = int(-(-pct * len(sorted_values) // 100)) - 1
    rank = max(0, min(rank, len(sorted_values) - 1))
    return sorted_values[rank]


def _aggregate_rows(rows: Iterable) -> dict | None:
    """Fold raw check_results rows into one rollup record, or None if empty.

    `rows` must yield mappings/sequences with `status` and `response_time_ms`.
    """
    check_count = 0
    ok_count = 0
    fail_count = 0
    latencies: list[int] = []
    latency_sum = 0
    for r in rows:
        check_count += 1
        if r["status"] == "up":
            ok_count += 1
        else:
            fail_count += 1
        rt = r["response_time_ms"]
        if rt is not None:
            latencies.append(rt)
            latency_sum += rt
    if check_count == 0:
        return None
    latencies.sort()
    avg = (latency_sum / len(latencies)) if latencies else None
    return {
        "check_count": check_count,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "avg_latency_ms": avg,
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "p99_latency_ms": _percentile(latencies, 99),
    }


async def _distinct_monitor_ids_in_window(
    db: aiosqlite.Connection, start_iso: str, end_iso: str
) -> list[str]:
    # INNER JOIN to monitors so check_results for already-deleted monitors
    # don't trip the rollup table's FK. Cascading delete normally cleans these
    # up, but historical data can have orphans we don't want to crash on.
    async with db.execute(
        """SELECT DISTINCT cr.monitor_id FROM check_results cr
            JOIN monitors m ON m.id = cr.monitor_id
            WHERE cr.checked_at >= ? AND cr.checked_at < ?""",
        (start_iso, end_iso),
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def compact_window(
    db: aiosqlite.Connection,
    tier: Tier,
    window_start: datetime,
) -> int:
    """Recompute rollup rows for the bucket beginning at `window_start`.

    Reads raw `check_results` for `[window_start, window_start + bucket)` and
    upserts one row per monitor that had any check in that window. Existing
    rollup rows for the same `(monitor_id, window_start)` are replaced, so this
    is safe to call repeatedly (e.g. on every scheduler tick).

    Returns the number of rollup rows written.
    """
    if tier == "raw":
        raise ValueError("cannot compact tier 'raw'")
    table = _TIER_TABLE[tier]
    bucket_seconds = _TIER_BUCKET_SECONDS[tier]
    window_start = floor_to_bucket(window_start, bucket_seconds)
    window_end = window_start + timedelta(seconds=bucket_seconds)
    start_iso = window_start.isoformat()
    end_iso = window_end.isoformat()

    monitor_ids = await _distinct_monitor_ids_in_window(db, start_iso, end_iso)
    if not monitor_ids:
        return 0

    written = 0
    for monitor_id in monitor_ids:
        async with db.execute(
            """SELECT status, response_time_ms FROM check_results
                WHERE monitor_id = ? AND checked_at >= ? AND checked_at < ?""",
            (monitor_id, start_iso, end_iso),
        ) as cur:
            rows = await cur.fetchall()
        agg = _aggregate_rows(rows)
        if agg is None:
            continue
        await db.execute(
            f"""INSERT OR REPLACE INTO {table}
                (monitor_id, window_start, check_count, ok_count, fail_count,
                 avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor_id,
                start_iso,
                agg["check_count"],
                agg["ok_count"],
                agg["fail_count"],
                agg["avg_latency_ms"],
                agg["p50_latency_ms"],
                agg["p95_latency_ms"],
                agg["p99_latency_ms"],
            ),
        )
        written += 1
    await db.commit()
    return written


# Per-tier "last fully compacted bucket" so the scheduler doesn't pointlessly
# rewrite the same 5m/1h rows every minute. Module-level state is fine — the
# scheduler runs in a single process; tests reset via `reset_compaction_state`.
_LAST_COMPACTED: dict[Tier, str | None] = {"1m": None, "5m": None, "1h": None}


def reset_compaction_state() -> None:
    """Forget what's been compacted. Tests use this to start clean."""
    for k in _LAST_COMPACTED:
        _LAST_COMPACTED[k] = None


async def compact_recent(db: aiosqlite.Connection, now: datetime | None = None) -> dict:
    """Roll up newly-elapsed buckets for each tier.

    Called on every scheduler minute tick. Skips tiers whose most recent
    fully-elapsed bucket has already been written by a prior tick. Idempotent
    on cold start — rewriting the same bucket is a no-op semantically.

    Returns ``{tier: rows_written}`` for tiers that actually fired.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    summary: dict[str, int] = {}
    floor_now_minute = floor_to_bucket(now, 60)

    for tier, bucket_seconds in _TIER_BUCKET_SECONDS.items():
        last_window = floor_to_bucket(now - timedelta(seconds=bucket_seconds), bucket_seconds)
        # Only consider fully-elapsed buckets.
        if last_window + timedelta(seconds=bucket_seconds) > floor_now_minute:
            continue
        key = last_window.isoformat()
        if _LAST_COMPACTED[tier] == key:
            continue
        summary[tier] = await compact_window(db, tier, last_window)
        _LAST_COMPACTED[tier] = key
    return summary


async def backfill(
    db: aiosqlite.Connection,
    tier: Tier,
    start: datetime,
    end: datetime,
) -> int:
    """Bucket-by-bucket recompute over `[start, end)`.

    Used by the standalone backfill script and tests. Stops at `end` (exclusive)
    so callers can pass `datetime.now()` without including the in-flight bucket.
    """
    if tier == "raw":
        raise ValueError("cannot backfill tier 'raw'")
    bucket_seconds = _TIER_BUCKET_SECONDS[tier]
    cursor = floor_to_bucket(start, bucket_seconds)
    end_floored = floor_to_bucket(end, bucket_seconds)
    total = 0
    while cursor < end_floored:
        total += await compact_window(db, tier, cursor)
        cursor += timedelta(seconds=bucket_seconds)
    return total


# ---------------------------------------------------------------------------
# Read helpers — these are what dashboard / digest call.
# ---------------------------------------------------------------------------


async def get_monitor_window_stats(
    db: aiosqlite.Connection,
    monitor_id: str,
    window_seconds: float,
    now: datetime | None = None,
) -> dict:
    """Aggregate stats over the trailing `window_seconds` for one monitor.

    Returns: ``{check_count, ok_count, fail_count, avg_latency_ms,
    min_latency_ms, max_latency_ms, uptime_pct}``.

    Picks the cheapest tier via `pick_tier`. For the raw tier it falls back to
    a direct scan of `check_results` so callers don't need to special-case it.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
    tier = pick_tier(window_seconds)

    if tier == "raw":
        async with db.execute(
            """SELECT
                   COUNT(*) AS checks,
                   SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS ok_count,
                   SUM(CASE WHEN status != 'up' THEN 1 ELSE 0 END) AS fail_count,
                   AVG(response_time_ms) AS avg_latency_ms,
                   MIN(response_time_ms) AS min_latency_ms,
                   MAX(response_time_ms) AS max_latency_ms
                FROM check_results
                WHERE monitor_id = ? AND checked_at >= ?""",
            (monitor_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
        checks = (row["checks"] if row else 0) or 0
        ok = (row["ok_count"] if row else 0) or 0
        fail = (row["fail_count"] if row else 0) or 0
        avg = row["avg_latency_ms"] if row else None
        return {
            "tier": tier,
            "check_count": checks,
            "ok_count": ok,
            "fail_count": fail,
            "avg_latency_ms": avg,
            "min_latency_ms": row["min_latency_ms"] if row else None,
            "max_latency_ms": row["max_latency_ms"] if row else None,
            "uptime_pct": round(ok / checks * 100, 2) if checks else 100.0,
        }

    table = _TIER_TABLE[tier]
    # MIN/MAX latency aren't carried in the rollup; we expose p50/p99 instead.
    async with db.execute(
        f"""SELECT
                COALESCE(SUM(check_count), 0) AS checks,
                COALESCE(SUM(ok_count), 0) AS ok_count,
                COALESCE(SUM(fail_count), 0) AS fail_count,
                CASE WHEN SUM(check_count) > 0
                    THEN SUM(avg_latency_ms * check_count) * 1.0 / SUM(check_count)
                    ELSE NULL END AS avg_latency_ms,
                MIN(p50_latency_ms) AS p50_latency_ms,
                MAX(p99_latency_ms) AS p99_latency_ms
            FROM {table}
            WHERE monitor_id = ? AND window_start >= ?""",
        (monitor_id, cutoff),
    ) as cur:
        row = await cur.fetchone()
    checks = row["checks"] or 0
    ok = row["ok_count"] or 0
    fail = row["fail_count"] or 0
    return {
        "tier": tier,
        "check_count": checks,
        "ok_count": ok,
        "fail_count": fail,
        "avg_latency_ms": row["avg_latency_ms"],
        "min_latency_ms": row["p50_latency_ms"],
        "max_latency_ms": row["p99_latency_ms"],
        "uptime_pct": round(ok / checks * 100, 2) if checks else 100.0,
    }


async def count_user_checks_in_window(
    db: aiosqlite.Connection,
    user_id: str,
    window_seconds: float,
    now: datetime | None = None,
) -> int:
    """Total checks across all of a user's monitors over the trailing window.

    Used by the dashboard "Checks · 24h" tile.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
    tier = pick_tier(window_seconds)
    if tier == "raw":
        async with db.execute(
            """SELECT COUNT(*) AS n FROM check_results c
                JOIN monitors m ON m.id = c.monitor_id
                WHERE m.user_id = ? AND c.checked_at >= ?""",
            (user_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
        return (row["n"] if row else 0) or 0
    table = _TIER_TABLE[tier]
    async with db.execute(
        f"""SELECT COALESCE(SUM(r.check_count), 0) AS n
                FROM {table} r
                JOIN monitors m ON m.id = r.monitor_id
                WHERE m.user_id = ? AND r.window_start >= ?""",
        (user_id, cutoff),
    ) as cur:
        row = await cur.fetchone()
    return (row["n"] if row else 0) or 0
