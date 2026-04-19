from __future__ import annotations

import logging
from datetime import datetime, timezone

import resend

from pingback.config import APP_BASE_URL, RESEND_API_KEY, RESEND_FROM_EMAIL
from pingback.db.connection import get_database
from pingback.db.digest import (
    get_user_digest_stats,
    get_users_due_for_digest,
    mark_digest_sent,
)

logger = logging.getLogger("pingback.email")


def _build_digest_html(user_name: str | None, stats: dict, unsubscribe_url: str) -> str:
    """Build a simple, inline-styled HTML email for the daily digest."""
    greeting = f"Hi {user_name}," if user_name else "Hi,"
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Overall summary row
    overall_color = "#22c55e" if stats["overall_uptime_pct"] >= 99 else (
        "#eab308" if stats["overall_uptime_pct"] >= 95 else "#ef4444"
    )

    monitor_rows = ""
    for m in stats["monitors"]:
        color = "#22c55e" if m["uptime_pct"] >= 99 else (
            "#eab308" if m["uptime_pct"] >= 95 else "#ef4444"
        )
        avg_ms = f'{m["avg_response_ms"]}ms' if m["avg_response_ms"] else "—"
        monitor_rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{m["name"]}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">
                <span style="color:{color};font-weight:600;">{m["uptime_pct"]}%</span>
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{m["checks"]}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{m["incidents"]}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{avg_ms}</td>
        </tr>"""

    incident_note = ""
    if stats["total_incidents"] > 0:
        incident_note = f"""
        <div style="background:#fef2f2;border-left:4px solid #ef4444;padding:12px 16px;margin:16px 0;border-radius:4px;">
            <strong style="color:#dc2626;">&#9888; {stats["total_incidents"]} incident(s)</strong> detected in the last 24 hours.
            Check your <a href="{APP_BASE_URL}" style="color:#2563eb;">dashboard</a> for details.
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
    <div style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <div style="background:#1e293b;padding:24px;text-align:center;">
            <h1 style="margin:0;color:#ffffff;font-size:20px;">Pingback Daily Digest</h1>
            <p style="margin:4px 0 0;color:#94a3b8;font-size:14px;">{date_str}</p>
        </div>
        <div style="padding:24px;">
            <p style="color:#374151;margin:0 0 16px;">{greeting}</p>
            <p style="color:#374151;margin:0 0 20px;">Here's your monitoring summary for the last 24 hours.</p>

            <div style="display:flex;gap:16px;margin-bottom:20px;">
                <div style="flex:1;background:#f9fafb;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:28px;font-weight:700;color:{overall_color};">{stats["overall_uptime_pct"]}%</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:4px;">Overall Uptime</div>
                </div>
                <div style="flex:1;background:#f9fafb;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:28px;font-weight:700;color:#1e293b;">{stats["total_checks"]}</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:4px;">Total Checks</div>
                </div>
                <div style="flex:1;background:#f9fafb;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:28px;font-weight:700;color:{"#ef4444" if stats["total_incidents"] > 0 else "#22c55e"};">{stats["total_incidents"]}</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:4px;">Incidents</div>
                </div>
            </div>

            {incident_note}

            <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:8px;">
                <thead>
                    <tr style="background:#f9fafb;">
                        <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Monitor</th>
                        <th style="padding:8px 12px;text-align:center;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Uptime</th>
                        <th style="padding:8px 12px;text-align:center;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Checks</th>
                        <th style="padding:8px 12px;text-align:center;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Incidents</th>
                        <th style="padding:8px 12px;text-align:center;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Avg Resp</th>
                    </tr>
                </thead>
                <tbody>{monitor_rows}
                </tbody>
            </table>

            <div style="text-align:center;margin-top:24px;">
                <a href="{APP_BASE_URL}" style="display:inline-block;background:#2563eb;color:#ffffff;padding:10px 24px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">View Dashboard</a>
            </div>
        </div>
        <div style="background:#f9fafb;padding:16px 24px;text-align:center;border-top:1px solid #e5e7eb;">
            <p style="margin:0;color:#9ca3af;font-size:12px;">
                You're receiving this because you opted in to daily digests.
                <a href="{unsubscribe_url}" style="color:#6b7280;">Unsubscribe</a>
            </p>
        </div>
    </div>
</div>
</body>
</html>"""


async def send_daily_digests(current_hour_utc: int) -> int:
    """Send digest emails to all eligible users for the given UTC hour. Returns count sent."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping digest send")
        return 0

    resend.api_key = RESEND_API_KEY
    db = await get_database()
    users = await get_users_due_for_digest(db, current_hour_utc)

    if not users:
        return 0

    sent = 0
    for user in users:
        try:
            stats = await get_user_digest_stats(db, user["id"])
            if stats["total_checks"] == 0:
                # No checks in the last 24h — nothing useful to report
                await mark_digest_sent(db, user["id"])
                continue

            unsubscribe_url = f"{APP_BASE_URL}/api/digest/unsubscribe?token={user['unsubscribe_token']}"
            html = _build_digest_html(user["name"], stats, unsubscribe_url)
            date_str = datetime.now(timezone.utc).strftime("%b %d")

            resend.Emails.send({
                "from": RESEND_FROM_EMAIL,
                "to": [user["email"]],
                "subject": f"Pingback Digest — {stats['overall_uptime_pct']}% uptime — {date_str}",
                "html": html,
                "headers": {
                    "List-Unsubscribe": f"<{unsubscribe_url}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            })

            await mark_digest_sent(db, user["id"])
            sent += 1
            logger.info("Sent digest to user %s", user["id"])
        except Exception:
            logger.exception("Failed to send digest to user %s", user["id"])

    logger.info("Daily digest complete: %d/%d emails sent", sent, len(users))
    return sent
