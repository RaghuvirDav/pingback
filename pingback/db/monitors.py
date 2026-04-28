from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from pingback.models import CheckResult, CheckStatus, Monitor, MonitorWithLastCheck

logger = logging.getLogger("pingback.db.monitors")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_monitor(
    db: aiosqlite.Connection,
    user_id: str,
    name: str,
    url: str,
    interval_seconds: int = 300,
    is_public: bool = False,
) -> Monitor:
    monitor_id = str(uuid.uuid4())
    now = _now_iso()
    await db.execute(
        """INSERT INTO monitors (id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
        (monitor_id, user_id, name, url, interval_seconds, int(is_public), now, now),
    )
    await db.commit()
    return Monitor(
        id=monitor_id,
        user_id=user_id,
        name=name,
        url=url,
        interval_seconds=interval_seconds,
        status="active",
        is_public=is_public,
        created_at=now,
        updated_at=now,
    )


async def count_user_monitors(db: aiosqlite.Connection, user_id: str) -> int:
    """Return the total number of monitors owned by a user."""
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM monitors WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def find_monitor_by_id(db: aiosqlite.Connection, monitor_id: str) -> Optional[Monitor]:
    async with db.execute(
        "SELECT id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at FROM monitors WHERE id = ?",
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
        is_public=bool(row["is_public"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def find_monitors_by_user(db: aiosqlite.Connection, user_id: str) -> list[Monitor]:
    async with db.execute(
        "SELECT id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at FROM monitors WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        Monitor(
            id=r["id"], user_id=r["user_id"], name=r["name"], url=r["url"],
            interval_seconds=r["interval_seconds"], status=r["status"],
            is_public=bool(r["is_public"]),
            created_at=r["created_at"], updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def find_active_monitors(db: aiosqlite.Connection) -> list[Monitor]:
    async with db.execute(
        "SELECT id, user_id, name, url, interval_seconds, status, is_public, created_at, updated_at FROM monitors WHERE status = 'active'"
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        Monitor(
            id=r["id"], user_id=r["user_id"], name=r["name"], url=r["url"],
            interval_seconds=r["interval_seconds"], status=r["status"],
            is_public=bool(r["is_public"]),
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
    """Return uptime percentage over the last 30 days (0.0–100.0).

    Reads from the 1h rollup tier (720 rows / 30d / monitor) instead of raw
    check_results. See `pingback.db.rollups` for the tier dispatch logic.
    """
    from pingback.db.rollups import get_monitor_window_stats

    stats = await get_monitor_window_stats(db, monitor_id, 30 * 86400)
    return stats["uptime_pct"]


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


async def archive_abandoned_free_accounts(db: aiosqlite.Connection, inactivity_days: int) -> int:
    """Pause monitors and delete check history for free-tier users inactive for inactivity_days.

    A user is considered abandoned when their last_login_at (or created_at if
    they never logged in) is older than the cutoff.  Returns the number of
    affected user accounts.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=inactivity_days)).isoformat()

    # Find abandoned free-tier users
    async with db.execute(
        """SELECT id FROM users
           WHERE plan = 'free'
             AND COALESCE(last_login_at, created_at) < ?""",
        (cutoff,),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return 0

    user_ids = [r["id"] for r in rows]

    for uid in user_ids:
        # Delete check_results (raw + rollups) for all monitors owned by this user
        for table in ("check_results", "check_results_1m", "check_results_5m", "check_results_1h"):
            await db.execute(
                f"""DELETE FROM {table}
                   WHERE monitor_id IN (SELECT id FROM monitors WHERE user_id = ?)""",
                (uid,),
            )
        # Pause all active monitors
        await db.execute(
            """UPDATE monitors SET status = 'paused', updated_at = ?
               WHERE user_id = ? AND status = 'active'""",
            (_now_iso(), uid),
        )
        # Audit log entry
        await db.execute(
            """INSERT INTO audit_log (id, user_id, action, resource_type, detail, timestamp)
               VALUES (?, ?, 'archive_abandoned', 'account', ?, ?)""",
            (
                str(uuid.uuid4()),
                uid,
                f"Free-tier account inactive for {inactivity_days}+ days; monitors paused, check history deleted",
                _now_iso(),
            ),
        )

    await db.commit()
    logger.info(
        "Archived %d abandoned free-tier account(s) (inactive > %d days)",
        len(user_ids),
        inactivity_days,
    )
    return len(user_ids)


async def purge_expired_check_results(db: aiosqlite.Connection, retention_days: int) -> int:
    """Delete check_results older than each monitor-owner's plan retention.

    `retention_days` is used as the fallback when a user has no known plan and
    as the default for the Pro/Business tiers. Free-plan users keep a shorter
    window defined by `HISTORY_DAYS_FREE`.
    """
    from pingback.services.plans import limits_for

    now = datetime.now(timezone.utc)
    total_deleted = 0

    # Collect the distinct plans currently in use so a new plan added later
    # is purged automatically via limits_for().
    async with db.execute("SELECT DISTINCT COALESCE(plan, 'free') FROM users") as cur:
        plans = [row[0] for row in await cur.fetchall()]
    if not plans:
        plans = ["free"]

    for plan in plans:
        days = limits_for(plan).history_days
        # `retention_days` is the operator-configured ceiling; never keep data
        # longer than it allows even if the plan default is higher.
        effective = min(days, retention_days)
        cutoff = (now - timedelta(days=effective)).isoformat()
        cursor = await db.execute(
            """DELETE FROM check_results
                WHERE checked_at < ?
                  AND monitor_id IN (
                      SELECT m.id FROM monitors m
                      JOIN users u ON u.id = m.user_id
                      WHERE COALESCE(u.plan, 'free') = ?
                  )""",
            (cutoff, plan),
        )
        deleted = cursor.rowcount or 0
        total_deleted += deleted
        if deleted > 0:
            logger.info(
                "Purged %d check_results older than %d days for plan=%s",
                deleted,
                effective,
                plan,
            )
        # MAK-147: keep rollups in lockstep with raw retention.
        for table in ("check_results_1m", "check_results_5m", "check_results_1h"):
            await db.execute(
                f"""DELETE FROM {table}
                    WHERE window_start < ?
                      AND monitor_id IN (
                          SELECT m.id FROM monitors m
                          JOIN users u ON u.id = m.user_id
                          WHERE COALESCE(u.plan, 'free') = ?
                      )""",
                (cutoff, plan),
            )

    await db.commit()
    return total_deleted
