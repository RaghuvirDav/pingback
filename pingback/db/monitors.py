from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from pingback.models import CheckResult, CheckStatus, Monitor, MonitorWithLastCheck


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_monitor(
    db: aiosqlite.Connection,
    user_id: str,
    name: str,
    url: str,
    interval_seconds: int = 300,
) -> Monitor:
    monitor_id = str(uuid.uuid4())
    now = _now_iso()
    await db.execute(
        """INSERT INTO monitors (id, user_id, name, url, interval_seconds, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (monitor_id, user_id, name, url, interval_seconds, now, now),
    )
    await db.commit()
    return Monitor(
        id=monitor_id,
        user_id=user_id,
        name=name,
        url=url,
        interval_seconds=interval_seconds,
        status="active",
        created_at=now,
        updated_at=now,
    )


async def find_monitor_by_id(db: aiosqlite.Connection, monitor_id: str) -> Optional[Monitor]:
    async with db.execute(
        "SELECT id, user_id, name, url, interval_seconds, status, created_at, updated_at FROM monitors WHERE id = ?",
        (monitor_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return Monitor(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        url=row["url"],
        interval_seconds=row["interval_seconds"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def find_monitors_by_user(db: aiosqlite.Connection, user_id: str) -> list[Monitor]:
    async with db.execute(
        "SELECT id, user_id, name, url, interval_seconds, status, created_at, updated_at FROM monitors WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        Monitor(
            id=r["id"], user_id=r["user_id"], name=r["name"], url=r["url"],
            interval_seconds=r["interval_seconds"], status=r["status"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def find_active_monitors(db: aiosqlite.Connection) -> list[Monitor]:
    async with db.execute(
        "SELECT id, user_id, name, url, interval_seconds, status, created_at, updated_at FROM monitors WHERE status = 'active'"
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        Monitor(
            id=r["id"], user_id=r["user_id"], name=r["name"], url=r["url"],
            interval_seconds=r["interval_seconds"], status=r["status"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def delete_monitor(db: aiosqlite.Connection, monitor_id: str) -> bool:
    cursor = await db.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    await db.commit()
    return cursor.rowcount > 0


async def save_check_result(
    db: aiosqlite.Connection,
    monitor_id: str,
    status: CheckStatus,
    status_code: Optional[int],
    response_time_ms: Optional[int],
    error: Optional[str],
) -> CheckResult:
    check_id = str(uuid.uuid4())
    now = _now_iso()
    await db.execute(
        """INSERT INTO check_results (id, monitor_id, status, status_code, response_time_ms, error, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (check_id, monitor_id, status, status_code, response_time_ms, error, now),
    )
    await db.commit()
    return CheckResult(
        id=check_id,
        monitor_id=monitor_id,
        status=status,
        status_code=status_code,
        response_time_ms=response_time_ms,
        error=error,
        checked_at=now,
    )


async def get_last_check(db: aiosqlite.Connection, monitor_id: str) -> Optional[CheckResult]:
    async with db.execute(
        "SELECT id, monitor_id, status, status_code, response_time_ms, error, checked_at FROM check_results WHERE monitor_id = ? ORDER BY checked_at DESC LIMIT 1",
        (monitor_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return CheckResult(
        id=row["id"], monitor_id=row["monitor_id"], status=row["status"],
        status_code=row["status_code"], response_time_ms=row["response_time_ms"],
        error=row["error"], checked_at=row["checked_at"],
    )


async def get_check_history(
    db: aiosqlite.Connection, monitor_id: str, limit: int = 100
) -> list[CheckResult]:
    async with db.execute(
        "SELECT id, monitor_id, status, status_code, response_time_ms, error, checked_at FROM check_results WHERE monitor_id = ? ORDER BY checked_at DESC LIMIT ?",
        (monitor_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        CheckResult(
            id=r["id"], monitor_id=r["monitor_id"], status=r["status"],
            status_code=r["status_code"], response_time_ms=r["response_time_ms"],
            error=r["error"], checked_at=r["checked_at"],
        )
        for r in rows
    ]


async def get_30day_uptime(db: aiosqlite.Connection, monitor_id: str) -> float:
    """Return uptime percentage over the last 30 days (0.0–100.0)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with db.execute(
        """SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS up_count
           FROM check_results
           WHERE monitor_id = ? AND checked_at >= ?""",
        (monitor_id, cutoff),
    ) as cursor:
        row = await cursor.fetchone()
    total = row["total"]
    if total == 0:
        return 100.0
    return round(row["up_count"] / total * 100, 2)


async def get_response_times(
    db: aiosqlite.Connection, monitor_id: str, limit: int = 50
) -> list[dict]:
    """Return recent response times as [{checked_at, response_time_ms}]."""
    async with db.execute(
        """SELECT checked_at, response_time_ms
           FROM check_results
           WHERE monitor_id = ? AND response_time_ms IS NOT NULL
           ORDER BY checked_at DESC LIMIT ?""",
        (monitor_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"checked_at": r["checked_at"], "response_time_ms": r["response_time_ms"]} for r in reversed(rows)]


async def find_monitors_with_last_check(
    db: aiosqlite.Connection, user_id: str
) -> list[MonitorWithLastCheck]:
    monitors = await find_monitors_by_user(db, user_id)
    result = []
    for m in monitors:
        last_check = await get_last_check(db, m.id)
        result.append(MonitorWithLastCheck(**m.model_dump(), last_check=last_check))
    return result
