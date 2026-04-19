from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from jinja2 import Template

from pingback.db.connection import get_database
from pingback.db.monitors import (
    find_monitors_with_last_check,
    get_30day_uptime,
    get_response_times,
)

router = APIRouter()

STATUS_PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Status{% if user_id %} &mdash; {{ user_id }}{% endif %}</title>
<style>
  :root {
    --green: #22c55e; --red: #ef4444; --yellow: #eab308;
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #f8fafc; --muted: #94a3b8;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; padding: 2rem 1rem; }
  .container { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
  .subtitle { color: var(--muted); font-size: 0.875rem; margin-bottom: 1.5rem; }
  .overall { padding: 1rem 1.25rem; border-radius: 0.5rem; margin-bottom: 2rem; font-weight: 600; font-size: 1.1rem; }
  .overall.operational { background: color-mix(in srgb, var(--green) 15%, transparent); color: var(--green); border: 1px solid color-mix(in srgb, var(--green) 30%, transparent); }
  .overall.partial    { background: color-mix(in srgb, var(--yellow) 15%, transparent); color: var(--yellow); border: 1px solid color-mix(in srgb, var(--yellow) 30%, transparent); }
  .overall.major      { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); border: 1px solid color-mix(in srgb, var(--red) 30%, transparent); }
  .monitor { background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem; padding: 1rem 1.25rem; margin-bottom: 1rem; }
  .monitor-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
  .monitor-name { font-weight: 600; }
  .badge { padding: 0.15rem 0.6rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
  .badge.up    { background: color-mix(in srgb, var(--green) 20%, transparent); color: var(--green); }
  .badge.down  { background: color-mix(in srgb, var(--red) 20%, transparent); color: var(--red); }
  .badge.error { background: color-mix(in srgb, var(--yellow) 20%, transparent); color: var(--yellow); }
  .badge.unknown { background: var(--border); color: var(--muted); }
  .meta { color: var(--muted); font-size: 0.8rem; display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
  .chart-label { color: var(--muted); font-size: 0.75rem; margin-bottom: 0.25rem; }
  .chart { display: flex; align-items: flex-end; gap: 2px; height: 40px; }
  .bar { flex: 1; min-width: 3px; max-width: 10px; background: var(--green); border-radius: 1px 1px 0 0; transition: background 0.2s; }
  .bar:hover { opacity: 0.8; }
  .empty { color: var(--muted); font-size: 0.8rem; padding: 3rem 0; text-align: center; }
  footer { text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 2rem; }
</style>
</head>
<body>
<div class="container">
  <h1>Service Status</h1>
  <p class="subtitle">Real-time monitoring dashboard</p>

  {% if monitors %}
  <div class="overall {{ overall_class }}">{{ overall_label }}</div>
  {% endif %}

  {% if not monitors %}
  <div class="empty">No monitors configured.</div>
  {% endif %}

  {% for m in monitors %}
  <div class="monitor">
    <div class="monitor-header">
      <span class="monitor-name">{{ m.name }}</span>
      <span class="badge {{ m.current_status }}">{{ m.current_status }}</span>
    </div>
    <div class="meta">
      <span>Uptime (30d): <strong>{{ m.uptime }}%</strong></span>
      {% if m.last_response_ms is not none %}
      <span>Last response: <strong>{{ m.last_response_ms }} ms</strong></span>
      {% endif %}
      {% if m.last_checked %}
      <span>Checked: {{ m.last_checked }}</span>
      {% endif %}
    </div>
    {% if m.response_times %}
    <div class="chart-label">Response time</div>
    <div class="chart">
      {% for rt in m.response_times %}
      <div class="bar" style="height:{{ rt.height }}%" title="{{ rt.ms }} ms"></div>
      {% endfor %}
    </div>
    {% endif %}
  </div>
  {% endfor %}

  <footer>Powered by Pingback</footer>
</div>
</body>
</html>
""")


def _overall_status(monitors: list[dict]) -> tuple[str, str]:
    """Return (css_class, label) for the overall status banner."""
    if not monitors:
        return "operational", "All systems operational"
    statuses = [m["current_status"] for m in monitors]
    down_count = statuses.count("down") + statuses.count("error")
    if down_count == 0:
        return "operational", "All systems operational"
    if down_count == len(statuses):
        return "major", "Major outage"
    return "partial", "Partial outage"


@router.get("/status/{user_id}", response_class=HTMLResponse)
async def public_status_page(user_id: str):
    db = await get_database()
    monitors_with_checks = await find_monitors_with_last_check(db, user_id)

    if not monitors_with_checks:
        # Check if user exists at all
        async with db.execute("SELECT id FROM users WHERE id = ?", (user_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="User not found")

    view_monitors = []
    for mwc in monitors_with_checks:
        uptime = await get_30day_uptime(db, mwc.id)
        rts_raw = await get_response_times(db, mwc.id, limit=50)

        current_status = "unknown"
        last_response_ms = None
        last_checked = None
        if mwc.last_check:
            current_status = mwc.last_check.status
            last_response_ms = mwc.last_check.response_time_ms
            last_checked = mwc.last_check.checked_at

        # Normalise bar heights (0–100 %) for the chart
        response_times = []
        if rts_raw:
            max_ms = max(r["response_time_ms"] for r in rts_raw) or 1
            response_times = [
                {"ms": r["response_time_ms"], "height": max(5, int(r["response_time_ms"] / max_ms * 100))}
                for r in rts_raw
            ]

        view_monitors.append({
            "name": mwc.name,
            "current_status": current_status,
            "uptime": uptime,
            "last_response_ms": last_response_ms,
            "last_checked": last_checked,
            "response_times": response_times,
        })

    overall_class, overall_label = _overall_status(view_monitors)

    html = STATUS_PAGE_TEMPLATE.render(
        user_id=user_id,
        monitors=view_monitors,
        overall_class=overall_class,
        overall_label=overall_label,
    )
    return HTMLResponse(content=html)
