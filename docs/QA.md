# Pingback QA — Manual Test Plan

Target: production launch 2026-04-20. Execute this plan end-to-end in staging before every release.

## 0. Setup

```bash
export ENCRYPTION_KEY="$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
export DB_PATH=/tmp/pingback.qa.db
rm -f "$DB_PATH"
python3 -m uvicorn pingback.main:app --port 8000
```

Automated regression:

```bash
python3 -m pytest tests/ -v
# Expect: all tests green (currently 41 tests, 0 failures)
```

## 1. Smoke (black-box)

- [ ] `GET /` renders the new landing page (hero demo, feature grid, CTA, footer).
- [ ] `GET /static/app.css` returns 200 and starts with `/* PingBack — dark monochrome`.
- [ ] `GET /health` returns `{"status": "ok"}`.
- [ ] Tailwind CDN `cdn.tailwindcss.com` is NOT referenced anywhere in rendered HTML.
- [ ] Geist font loads (check devtools Network tab).

## 2. Sign-up & session (black-box)

- [ ] Sign up with a fresh email → redirects to `/dashboard?welcome=1` with welcome banner.
- [ ] Session cookie `pb_session` is `HttpOnly` and `SameSite=Lax`.
- [ ] Sign up again with the same email → 409 "An account with that email already exists."
- [ ] Sign up is case-insensitive: `Alice@example.com` collides with `alice@example.com`.
- [ ] Logout clears the session; `/dashboard` then redirects to `/login`.
- [ ] `/login` with a wrong API key → 401 with error banner.
- [ ] `/login` with a correct API key → lands on `/dashboard`.

## 3. Dashboard (black-box)

- [ ] Empty state: sign up and immediately see the "No monitors yet" empty card with 3 onboarding steps.
- [ ] After adding a monitor: status hero shows "All systems operational", monitor row with status dot, uptime %, last response, interval.
- [ ] Sparkline renders once at least 2 response-time samples exist.
- [ ] "New monitor" button (sidebar + header) both navigate to `/dashboard/monitors/new`.
- [ ] Sidebar highlights the active page (Overview / Monitors / Settings / Billing).

## 4. Monitor CRUD (black-box)

- [ ] Create a monitor with valid name + URL + interval.
- [ ] Name is required (HTML5 form validation).
- [ ] URL field rejects empty / non-URL via browser (`<input type="url" required>`).
- [ ] Edit a monitor's interval — verify the seg-control updates hidden input and submits the right value.
- [ ] Toggle "Show on public status page" and confirm the monitor appears/disappears on `/status/<user_id>`.
- [ ] Delete a monitor — confirm dialog fires, row disappears, history is gone.
- [ ] Free plan cap: create 3 monitors; the 4th shows a plan-limit error banner.

## 5. Public status page (black-box)

- [ ] `/status/<valid user id>` renders with the new dark design.
- [ ] Only monitors with `is_public = 1` appear.
- [ ] Overall banner reads "All systems operational" when all public monitors are `up`.
- [ ] With a `down`/`error` monitor, banner shows "Partial outage" or "Major outage" as appropriate.
- [ ] `/status/<bogus uuid>` → 404.
- [ ] Empty state renders "No public monitors" when the user has none flagged public.

## 6. Settings (black-box)

- [ ] `/dashboard/settings` shows email, name, plan, user ID.
- [ ] "Copy" button copies the status URL to clipboard.
- [ ] Toggle digest enabled → "Save preferences" → re-open page → toggle state persists.
- [ ] Change send hour → save → reload → the new hour is selected.
- [ ] Rotate API key → confirm prompt → old session invalidated.

## 7. Billing (black-box)

- [ ] Free user sees FREE plan marked "Current plan"; PRO CTA is clickable.
- [ ] PRO plan click → `/dashboard/billing/checkout` (without Stripe secret key, graceful failure; with key, redirect to Stripe).
- [ ] Business plan tile → `mailto:sales@example.com` link (matches template default).

## 8. Error pages

- [ ] `GET /totally-not-a-route` → 404 page with new design.
- [ ] Trigger a 500 (e.g. simulate an unhandled exception in a dev branch) → 500 page with new design.
- [ ] Error pages link back to `/dashboard` and `/`.

## 9. Responsive / accessibility (black-box)

- [ ] At viewport width < 900px: sidebar collapses; menu button appears in topbar; content stacks.
- [ ] Tab through the landing page — focus rings visible on every interactive element.
- [ ] Screen reader announces brand logo (`aria-label="Pingback"`) and primary CTAs.
- [ ] `prefers-reduced-motion: reduce` disables animations (verify with devtools rendering emulation).
- [ ] All forms accept keyboard-only submission (Enter key on inputs).

## 10. White-box / internal

- [ ] `pingback/config.py` reads `ENCRYPTION_KEY`, `DB_PATH`, `APP_ENV`, `APP_BASE_URL`, Stripe/Resend keys from env.
- [ ] With `APP_ENV=production`, `HTTPSRedirectMiddleware` issues 307 on `http://` requests.
- [ ] `hash_email()` is case-insensitive and whitespace-trimmed — verify via pytest.
- [ ] `hash_api_key()` output is 64 hex chars (SHA-256).
- [ ] Fernet encryption key is required in production; warning logged when absent.
- [ ] Rate limiter: 20 req / 60s / IP on auth-sensitive API endpoints (covered by `test_rate_limit.py`).
- [ ] Scheduler (`start_scheduler`) ticks every 10s, fires `check_url` for each active monitor whose next-check window has elapsed.
- [ ] Audit log middleware writes rows for every `/api/...` request with `resource_type`, `resource_id`, `ip_address`.

## 11. Security sanity

- [ ] API key is never rendered in any HTML response (grep production DOM).
- [ ] User B cannot read, edit, or delete User A's monitors via the HTML dashboard OR the JSON API (covered by `test_monitors.py::test_ownership_enforced` and `test_api.py::test_api_unauth_monitor_access_forbidden`).
- [ ] Session cookie is `HttpOnly`; `Secure` is expected in production (check `set_session` in `pingback/session.py` — currently NOT setting `secure=True`, see Production Readiness checklist).
- [ ] No secrets logged on stdout (grep uvicorn output after full scenario for email/api_key/stripe key patterns).

## 12. Data safety

- [ ] Delete account → all monitors + check history gone.
- [ ] `RETENTION_DAYS` purge runs on scheduler tick and drops `check_results` rows older than N days.
- [ ] `ABANDONED_ACCOUNT_DAYS` purge pauses monitors + clears check history for inactive free users.

## 13. Cross-browser (manual)

- [ ] Chrome 120+
- [ ] Safari 17+
- [ ] Firefox 120+
- [ ] Mobile Safari (iPhone), Chrome Android

Record pass/fail + environment in the launch ticket.
