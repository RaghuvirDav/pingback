# Pingback Production Readiness Checklist

Ship-day sign-off for the 2026-04-20 launch.

## Required environment variables

Name | Purpose | Default | Required for production?
-----|---------|---------|-------------------------
`ENCRYPTION_KEY` | Fernet key for email / API-key / session encryption. Generate via `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` | `""` (warning + plaintext) | **Yes**
`DB_PATH` | SQLite DB path | `pingback.db` | Yes — mount on persistent volume
`APP_ENV` | Set to `production` for HTTPS redirect middleware + auto-enables `Secure` cookies | `development` | **Yes**
`SESSION_COOKIE_SECURE` | Explicit override to flag the session cookie as `Secure` (HTTPS-only). Accepts `1`/`true`/`yes`/`on`. | unset | Yes if TLS is in front of the app and `APP_ENV` != `production`
`APP_BASE_URL` | Used in email + status URL generation | `http://localhost:8000` | **Yes**
`PORT` / `HOST` | Uvicorn bind | `8000` / `0.0.0.0` | No
`RETENTION_DAYS` | Days of check history to keep | `90` | No
`ABANDONED_ACCOUNT_DAYS` | Days inactive before free-tier monitor pause | `30` | No
`RESEND_API_KEY` + `RESEND_FROM_EMAIL` | Transactional email | empty | Yes if sending digest
`STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` + `STRIPE_PRO_PRICE_ID` | Billing | empty | Yes for paid plans
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` | SES/S3 | empty | Only if used

Secrets management: never commit `.env`. Use AWS Parameter Store / SSM or Docker secrets.

## Pre-flight checks

- [x] Automated tests green (`pytest` → 41 passed)
- [x] Landing page end-to-end smoke in TestClient (10 routes)
- [x] Rate limiter covered by unit test
- [x] Dashboard + monitor + status page all render with the new UI
- [x] Sign-up dedup bug fixed (`email_hash` UNIQUE index)
- [x] Public status page filters by `is_public` (was leaking all monitors)
- [ ] Manual QA checklist run in staging (`docs/QA.md`)
- [ ] Manual verification on Chrome + Safari + Mobile

## Logging

- `logging.basicConfig(level=INFO)` is set in `pingback.main`.
- Uvicorn access log is on by default.
- `pingback.email`, `pingback.scheduler`, `pingback.encryption` are all named loggers.

**Production adjustment:** pipe logs to stdout; Docker/ECS picks them up. If structured logs are desired, swap `logging.basicConfig` for a `logging.config.dictConfig` with a JSON formatter.

## Error handling

- 404 and 500 are served by custom templates (`pingback/templates/404.html`, `500.html`).
- `HTTPException` from dependency chains is already FastAPI-default → respects status codes.
- Checker service catches `httpx.TimeoutException` and generic `Exception` and records `down` / `error` respectively — no unhandled coroutine exits.

**Gap / follow-up (not blocking launch):** `HTTPSRedirectMiddleware` redirects `http://` → `https://` only when `APP_ENV=production`. Verify the reverse proxy (nginx / ALB) sets `X-Forwarded-Proto: https` so the middleware does not loop.

## Deploy path (Docker Compose)

```bash
# Local UAT smoke
docker compose -f docker-compose.yml up --build

# AWS free tier (EC2 or Lightsail)
docker compose -f docker-compose.aws.yml up -d --build
```

Checklist per deploy:

- [ ] Pin the application version (git SHA) in the image tag.
- [ ] Mount `DB_PATH` on a persistent volume (`/data/pingback.db`).
- [ ] Export the `ENCRYPTION_KEY` securely (AWS SSM / Docker secret). **Do not rotate without re-encryption plan.**
- [ ] Configure reverse proxy TLS (Caddy / ALB + ACM). Forward `X-Forwarded-Proto`.
- [ ] Enable health-check on `GET /health`.
- [ ] Set up daily SQLite backup (`cp $DB_PATH $BACKUPS/pingback-$(date +%Y%m%d).db`).

## Rollback plan

1. Keep the previous image tag available for 7 days.
2. Rollback = `docker compose up -d pingback:<previous-sha>`.
3. Schema migrations are all `IF NOT EXISTS` or idempotent `ALTER TABLE` with try/except — backwards compatible.
4. New `email_hash` column is additive; old rows default to NULL (not in UNIQUE index path). Safe to rollback.
5. Frontend change = templates + static CSS only; no breaking API changes.

## Monitoring / Observability

- `/health` for external ping (can be consumed by Pingback itself after boot!).
- Scheduler writes structured log lines on each tick (`INFO:pingback.scheduler:Scheduler started`).
- Audit log is queryable for business-plan users via `/api/audit-log`.

**Follow-up:** add Prometheus metrics or Sentry for production error tracking — not in scope for today's launch.

## Known limitations shipped today (tracked for post-launch)

- **Session cookie `Secure` flag is now env-gated.** Set `SESSION_COOKIE_SECURE=1` (or `APP_ENV=production`) in any deployment behind TLS. Default off for local dev so `http://localhost` still works. Covered by `tests/test_session_cookie.py`.
- **CSRF protection is deferred** — tracked in [MAK-52](/MAK/issues/MAK-52). `SameSite=Lax` + no cross-origin state-changing endpoints is an acceptable v1 risk per board sign-off on 2026-04-20.
- API keys are returned from signup but never re-shown in UI. Users can rotate via Settings but must save the key themselves — acceptable for MVP.
- Scheduler is in-process (single-instance). Horizontal scaling requires a distributed lock or a dedicated worker.

## Launch sign-off

Owner | Role | Sign-off
------|------|---------
agent-cto | CTO / engineer | ✅ automated tests + smoke pass
board | reviewer | pending
