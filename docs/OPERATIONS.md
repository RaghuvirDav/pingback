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
