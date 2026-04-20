# Pingback Operations Guide

Runtime operations, deploy, and observability notes. See
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for the launch checklist.

## Sentry error tracking (MAK-58)

Sentry is wired into the FastAPI app behind the `SENTRY_DSN` env var. When the
DSN is unset, `init_sentry()` is a no-op — safe for local dev and unit tests.

### Environment variables

Name | Purpose | Default
-----|---------|--------
`SENTRY_DSN` | Enables Sentry when set. Get it from Sentry → Project → Client Keys (DSN). | empty (disabled)
`SENTRY_TRACES_SAMPLE_RATE` | Fraction of transactions sent for performance tracing. Start low. | `0.1`
`SENTRY_ENVIRONMENT` | Tag applied to every event (`production`, `staging`, …). | falls back to `APP_ENV`
`SENTRY_RELEASE` | Optional release id (git SHA recommended). | empty
`DEBUG_BOOM_ENABLED` | Mounts `GET /debug/boom` so a deployer can smoke-test Sentry. | unset (disabled)

### First-time setup (board / CEO)

1. Create a free-tier Sentry org owned by `pingback@…` (shared mailbox) or a
   board member. Free tier gives 5k errors/month — more than we need today.
2. Create a project (`platform = python/fastapi`). Copy the DSN.
3. Drop the DSN into the deploy environment (AWS SSM or Docker secret —
   never commit). Typical variables set on the prod host:
   ```
   SENTRY_DSN=https://<public-key>@o<org-id>.ingest.sentry.io/<project-id>
   SENTRY_ENVIRONMENT=production
   SENTRY_RELEASE=<git-sha>
   ```
4. Deploy. Check the Sentry project receives the startup breadcrumb.

### PII scrubbing

`pingback/sentry_init.py` installs a `before_send` hook that:

- drops `user.email`, `user.ip_address`, `user.username`
- drops `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key` headers
- drops `request.cookies` and `env.REMOTE_ADDR`
- tags every event with `request_id` from the JSON-logging context var so a
  Sentry error can be cross-referenced with CloudWatch log lines

`send_default_pii=False` is set explicitly. Do not flip this on without a
privacy review.

### Smoke test

After deploy, enable the debug route for one request and hit it:

```
DEBUG_BOOM_ENABLED=1 ./deploy/restart.sh
curl -sS https://<host>/debug/boom    # → HTTP 500
# confirm the event shows up in the Sentry UI within ~30 s
DEBUG_BOOM_ENABLED=  ./deploy/restart.sh
```

Leave `DEBUG_BOOM_ENABLED` unset in normal prod — the route is gated at import
time so an unset flag means the route is not even registered.

## Uptime monitoring + public status page (MAK-59)

External uptime verification via UptimeRobot's free tier. Confirms that the
deployed app is reachable from outside AWS and gives us a free hosted status
page to share with users.

### Endpoint policy

- The external monitor hits **`GET /health`** — defined in
  `pingback/routes/health.py`. This must stay cheap: no DB calls, no external
  fetches. A bloated health check is a self-inflicted DoS every 5 minutes.
- If we ever want a richer "dependency health" view (DB ping, Sentry reachable,
  etc.), expose it on a separate path like `/status` with its own monitor.
  Do **not** overload `/health`.

### Account setup (board / CEO)

1. Sign up for UptimeRobot free tier at <https://uptimerobot.com/>. Use a
   shared mailbox (preferred) so credentials survive staff changes.
   Free tier gives 50 monitors at 5-min interval — plenty for us.
2. Verify the email, then store the account credentials in the password manager
   alongside the Sentry credentials.
3. Add any board members who should receive alerts as **alert contacts**
   (My Settings → Alert Contacts → Add). Email is fine for v1; we can wire
   Slack/PagerDuty later if the volume justifies it.

### Monitor configuration

Create one HTTPS monitor:

Field | Value
-----|------
Monitor Type | HTTPS
Friendly Name | `pingback-prod-health`
URL | `https://<prod-domain>/health`
Monitoring Interval | 5 minutes
Monitor Timeout | 30 seconds
HTTP Method | GET
Alert Contacts | all board contacts + `pingback@…` shared mailbox
Keyword monitoring | *(optional)* — keyword = `"status":"ok"`

### Public status page

1. UptimeRobot dashboard → **Status Pages → Add New Status Page**.
2. Name: `Pingback`. Select the `pingback-prod-health` monitor.
3. Visibility: **Public**. Copy the generated URL
   (`https://stats.uptimerobot.com/<id>`).
4. Paste that URL into the MAK-59 ticket and into this doc below under
   **"Live URLs"**.

### Custom domain (post domain-purchase follow-up)

Once the board's domain purchase lands, point a subdomain at the UptimeRobot
status page:

1. In UptimeRobot status page settings, add the custom domain
   `status.<domain>`.
2. Create a CNAME record: `status.<domain>` → `stats.uptimerobot.com`.
3. Wait for DNS propagation, then verify in UptimeRobot.

### Acceptance

- UptimeRobot dashboard shows `pingback-prod-health` green for > 24 h.
- Public status page URL is pasted into the MAK-59 ticket and into the
  **Live URLs** table below.

### Live URLs

Populate these once the account is live.

Name | URL
-----|----
UptimeRobot dashboard | *(board-only, keep in password manager)*
Public status page | *pending*
Custom status domain | *pending domain purchase*
