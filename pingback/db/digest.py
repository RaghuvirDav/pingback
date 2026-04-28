from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from pingback.encryption import decrypt_value

logger = logging.getLogger("pingback.digest.db")


def _resolve_tz(name: str | None) -> ZoneInfo:
    """Look up an IANA tz, falling back to UTC if the name is bad. We never
    want a junky DB row to wedge the whole digest run."""
    if not name:
        return ZoneInfo("Etc/UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %r; falling back to UTC", name)
        return ZoneInfo("Etc/UTC")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Digest preference CRUD
# ---------------------------------------------------------------------------

async def get_digest_pref(db: aiosqlite.Connection, user_id: str) -> Optional[dict]:
    async with db.execute(
        "SELECT user_id, enabled, send_hour_utc, unsubscribe_token, last_sent_at, created_at, updated_at "
        "FROM digest_preferences WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def upsert_digest_pref(
    db: aiosqlite.Connection,
    user_id: str,
    enabled: bool = True,
    send_hour_utc: int = 8,
) -> dict:
    now = _now_iso()
    existing = await get_digest_pref(db, user_id)
    if existing is not None:
        await db.execute(
            "UPDATE digest_preferences SET enabled = ?, send_hour_utc = ?, updated_at = ? WHERE user_id = ?",
            (int(enabled), send_hour_utc, now, user_id),
        )
        await db.commit()
        return {**existing, "enabled": int(enabled), "send_hour_utc": send_hour_utc, "updated_at": now}

    token = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO digest_preferences (user_id, enabled, send_hour_utc, unsubscribe_token, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, int(enabled), send_hour_utc, token, now, now),
    )
    await db.commit()
    return {
        "user_id": user_id,
        "enabled": int(enabled),
        "send_hour_utc": send_hour_utc,
        "unsubscribe_token": token,
        "last_sent_at": None,
        "created_at": now,
        "updated_at": now,
    }


async def disable_digest_by_token(db: aiosqlite.Connection, token: str) -> bool:
    now = _now_iso()
    cursor = await db.execute(
        "UPDATE digest_preferences SET enabled = 0, updated_at = ? WHERE unsubscribe_token = ?",
        (now, token),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_digest_sent(db: aiosqlite.Connection, user_id: str) -> None:
    now = _now_iso()
    await db.execute(
        "UPDATE digest_preferences SET last_sent_at = ?, updated_at = ? WHERE user_id = ?",
        (now, now, user_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Digest data queries
# ---------------------------------------------------------------------------

async def get_users_due_for_digest(
    db: aiosqlite.Connection, now_utc: datetime
) -> list[dict]:
    """Return users due for a digest right now, evaluated against each user's
    local timezone.

    A user is due when their preferred send hour (interpreted as local time
    in `users.timezone`) has already arrived today and we haven't already sent
    them today's digest. We compare with `>=` rather than `==` so that a
    delayed scheduler tick (or a service restart that crosses the user's
    preferred hour) still produces the email later that same local day,
    instead of silently dropping it the way the UTC-equality filter did
    before MAK-124.
    """
    async with db.execute(
        """
        SELECT u.id, u.email, u.name, u.timezone,
               dp.unsubscribe_token, dp.send_hour_utc, dp.last_sent_at
        FROM digest_preferences dp
        JOIN users u ON u.id = dp.user_id
        WHERE dp.enabled = 1
          AND u.consent_given_at IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM monitors m
              WHERE m.user_id = u.id AND m.status = 'active'
          )
        """
    ) as cursor:
        rows = await cursor.fetchall()

    now_utc = now_utc.astimezone(timezone.utc)
    eligible: list[dict] = []
    for r in rows:
        tz = _resolve_tz(r["timezone"])
        local_now = now_utc.astimezone(tz)
        send_hour = int(r["send_hour_utc"])
        if local_now.hour < send_hour:
            continue
        local_today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        last_sent_at = r["last_sent_at"]
        if last_sent_at:
            try:
                last = datetime.fromisoformat(last_sent_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last.astimezone(tz) >= local_today_start:
                    continue
            except ValueError:
                logger.warning(
                    "Bad last_sent_at %r for user %s; treating as never sent",
                    last_sent_at,
                    r["id"],
                )
        eligible.append(
            {
                "id": r["id"],
                "email": decrypt_value(r["email"]),
                "name": r["name"],
                "unsubscribe_token": r["unsubscribe_token"],
                "timezone": str(tz),
            }
        )
    return eligible


async def get_user_digest_stats(db: aiosqlite.Connection, user_id: str) -> dict:
    """Gather 24-hour digest stats for a user: per-monitor breakdown and overall summary."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Get active monitors
    async with db.execute(
        "SELECT id, name, url FROM monitors WHERE user_id = ? AND status = 'active' ORDER BY name",
        (user_id,),
    ) as cursor:
        monitors = await cursor.fetchall()

    total_checks = 0
    total_up = 0
    total_incidents = 0
    monitor_stats = []

    for mon in monitors:
        async with db.execute(
            """
            SELECT
                COUNT(*) AS checks,
                SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS up_count,
                SUM(CASE WHEN status != 'up' THEN 1 ELSE 0 END) AS incident_count,
                AVG(response_time_ms) AS avg_response_ms,
                MIN(response_time_ms) AS min_response_ms,
                MAX(response_time_ms) AS max_response_ms
            FROM check_results
            WHERE monitor_id = ? AND checked_at >= ?
            """,
            (mon["id"], cutoff),
        ) as cursor:
            row = await cursor.fetchone()

        checks = row["checks"] or 0
        up = row["up_count"] or 0
        incidents = row["incident_count"] or 0
        uptime_pct = round(up / checks * 100, 2) if checks > 0 else 100.0

        total_checks += checks
        total_up += up
        total_incidents += incidents

        monitor_stats.append({
            "name": mon["name"],
            "url": mon["url"],
            "checks": checks,
            "uptime_pct": uptime_pct,
            "incidents": incidents,
            "avg_response_ms": round(row["avg_response_ms"]) if row["avg_response_ms"] else None,
            "min_response_ms": row["min_response_ms"],
            "max_response_ms": row["max_response_ms"],
        })

    overall_uptime = round(total_up / total_checks * 100, 2) if total_checks > 0 else 100.0

    return {
        "total_checks": total_checks,
        "overall_uptime_pct": overall_uptime,
        "total_incidents": total_incidents,
        "monitors": monitor_stats,
    }
