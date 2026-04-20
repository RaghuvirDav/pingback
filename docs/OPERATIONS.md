# Pingback Operations Guide

Runtime operations, deploy, and observability notes. See
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for the launch checklist.

## CloudWatch Logs: retention + metric filters (MAK-60)

Pingback ships container stdout/stderr to CloudWatch via the Docker `awslogs`
driver (`docker-compose.aws.yml`). The log group is `pingback` in `us-east-1`.

### Retention — 14 days, non-negotiable

Free-tier CloudWatch Logs gives 5 GB/month of ingest. 14-day retention keeps
storage comfortably inside that budget long-term. The CEO called it
non-negotiable; we enforce it in code so console drift can't undo it.

`deploy/cloudwatch-setup.sh` is the source of truth. It is idempotent and is
called automatically from `deploy/setup-ec2.sh` when AWS CLI creds are
available. Re-run it any time:

```
AWS_REGION=us-east-1 bash deploy/cloudwatch-setup.sh
```

Verify:

```
aws logs describe-log-groups --log-group-name-prefix pingback \
  --query 'logGroups[].{name:logGroupName,retentionInDays:retentionInDays}'
# → retentionInDays: 14
```

### Metric filters (custom metrics → `Pingback/Logs`)

The setup script installs two JSON metric filters against the `pingback`
log group:

Filter | Pattern | Metric
-----|---------|-------
`ErrorCount` | `{ $.level = "ERROR" }` | `Pingback/Logs/ErrorCount`
`SchedulerFailureCount` | `{ $.level = "ERROR" && $.logger = "pingback.scheduler" }` | `Pingback/Logs/SchedulerFailureCount`

Both publish `value=1` per matching log record with `defaultValue=0`, so you
can chart zero-traffic periods without broken lines. These metrics are what
MAK-62 (alarms) subscribes to — do not rename them without updating that
ticket.

### Saved Logs Insights queries

`cloudwatch-setup.sh` upserts three query definitions (visible under
CloudWatch → Logs Insights → *Saved queries*):

Name | What it shows
-----|--------------
`Pingback/errors-last-hour` | All `level=ERROR` records, newest first
`Pingback/5xx-by-path` | HTTP 5xx count grouped by `path`
`Pingback/scheduler-failures` | Scheduler errors (`logger=pingback.scheduler`)

Raw query strings (paste into Logs Insights if the saved definition is not
yet installed on a fresh account):

```
fields @timestamp, level, logger, message, request_id, path, status
| filter level = "ERROR"
| sort @timestamp desc
| limit 200
```

```
fields @timestamp, path, status, request_id, duration_ms
| filter status >= 500
| stats count() as count by path
| sort count desc
```

```
fields @timestamp, message, request_id
| filter logger = "pingback.scheduler" and level = "ERROR"
| sort @timestamp desc
| limit 200
```

### IAM — minimum perms for the EC2 instance role

```
logs:CreateLogGroup
logs:PutRetentionPolicy
logs:DescribeLogGroups
logs:PutMetricFilter
logs:DescribeMetricFilters
logs:PutQueryDefinition
logs:DescribeQueryDefinitions
logs:CreateLogStream
logs:PutLogEvents
```

Scoped to `arn:aws:logs:us-east-1:<account>:log-group:pingback:*` plus a
blanket `logs:PutQueryDefinition`/`Describe*` on `*` (query definitions are
account-scoped, not log-group-scoped).

## CloudWatch alarms → SNS → board email (MAK-62)

Five alarms publish to one SNS topic. The free tier covers 10 alarms; we use 5
so there is headroom before we start paying. `deploy/cloudwatch-alarms.sh` is
the source of truth — idempotent, safe to re-run.

### SNS topic

Name | Region | Purpose
-----|--------|--------
`pingback-alarms` | `us-east-1` | Fan-out for every Pingback CloudWatch alarm (ALARM and OK transitions).

Subscribers are plain email (one per board member + the `pingback@…` shared
mailbox). Each address must click the confirmation link AWS sends before it
starts receiving alarms. List subscribers with:

```
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:<account>:pingback-alarms \
  --query 'Subscriptions[].[Protocol,Endpoint,SubscriptionArn]' --output table
```

### Alarms

Name | Metric | Condition | Why
-----|--------|-----------|----
`Pingback/ErrorRateHigh` | `Pingback/Logs/ErrorCount` | `Sum > 5` over 5 min | Spike of app ERROR lines (fed by MAK-60 metric filter)
`Pingback/SchedulerFailure` | `Pingback/Logs/SchedulerFailureCount` | `Sum >= 1` over 5 min | Any scheduler failure is worth a page
`Pingback/HealthCheckMissing` | `AWS/EC2 StatusCheckFailed` | `Max >= 1` for 2 of 3 minutes | Host unreachable; UptimeRobot remains the canonical external up/down
`Pingback/DiskSpaceLow` | `CWAgent disk_used_percent` (root fs) | `Avg > 80` over 5 min | SQLite DB + backups creep up over time
`Pingback/CpuHigh` | `AWS/EC2 CPUUtilization` | `Avg > 80` for 10 min | Sustained saturation, not a spike

`ErrorRateHigh` and `SchedulerFailure` use `treat-missing-data=notBreaching`
so zero-traffic periods don't self-alarm. `HealthCheckMissing` uses
`breaching` so a host that stops reporting status checks pages us.
`DiskSpaceLow` uses `missing` so a CW-agent outage doesn't mask a real
low-disk condition — verify the agent is running if it stays INSUFFICIENT_DATA.

### CloudWatch agent (prereq for `DiskSpaceLow`)

The two AWS/EC2 built-ins (CPU, StatusCheck) ship for free with every EC2
instance. `disk_used_percent` requires the CloudWatch agent. Install once:

```
# Amazon Linux 2023
sudo dnf install -y amazon-cloudwatch-agent

# Ubuntu
sudo apt-get install -y amazon-cloudwatch-agent
```

Drop this minimum config at
`/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json`:

```json
{
  "metrics": {
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "metrics_collected": {
      "disk": {
        "measurement": ["used_percent"],
        "resources": ["/"],
        "metrics_collection_interval": 300
      }
    }
  }
}
```

Then:

```
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
    -s
```

The instance role needs the managed policy
`CloudWatchAgentServerPolicy` on top of the existing `logs:*` perms.

If the root device/fstype on the host differs from the default
(`xvda1` / `xfs`), export `PINGBACK_DISK_DEVICE` / `PINGBACK_DISK_FSTYPE`
before running the alarms script. Confirm on the host with `df -T /`.

### Running the alarms script

```
export PINGBACK_INSTANCE_ID=i-0abc123def4567890
export ALERT_EMAILS="board@usepingback.com,pingback@usepingback.com"
bash deploy/cloudwatch-alarms.sh
```

Verify the alarms landed:

```
aws cloudwatch describe-alarms --region us-east-1 \
  --alarm-name-prefix Pingback/ \
  --query 'MetricAlarms[].[AlarmName,StateValue]' --output table
```

Right after first run they will all be `INSUFFICIENT_DATA` — that's expected.
Within 15 minutes CPU/StatusCheck transition to `OK`; ErrorRate/Scheduler
stay `OK` as long as no matching log lines arrive; `DiskSpaceLow` transitions
to `OK` as soon as the CW agent emits a data point.

### IAM — minimum perms for the deployer running this script

```
sns:CreateTopic
sns:Subscribe
sns:ListSubscriptionsByTopic
cloudwatch:PutMetricAlarm
cloudwatch:DescribeAlarms
```

The EC2 instance role does NOT need these — they only belong to whoever runs
`cloudwatch-alarms.sh` (board member from a laptop, or a CI deploy role).

### End-to-end test (Acceptance for MAK-62)

1. Confirm every subscriber clicked the AWS confirmation email. Unconfirmed
   subs silently drop alarms.
2. On a deployed host, enable the boom route and hammer it 6+ times in
   under 5 minutes:
   ```
   DEBUG_BOOM_ENABLED=1 ./deploy/restart.sh
   for i in $(seq 1 8); do curl -fsS https://<host>/debug/boom || true; done
   ```
3. Within ~2–3 minutes, `Pingback/ErrorRateHigh` transitions to `ALARM` and
   every subscriber receives an email. Record the test in the MAK-62 ticket.
4. Disable the route again: `DEBUG_BOOM_ENABLED= ./deploy/restart.sh`.

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
