# Gmail "Send mail as" via Resend SMTP — `hello@usepingback.com`

Lets the board reply from Gmail with the From address showing as
`hello@usepingback.com` (instead of `davraghuvir9@gmail.com`).
Companion to [MAK-175](/MAK/issues/MAK-175); inbound forwarder set up in
[MAK-173](/MAK/issues/MAK-173).

## Prerequisites

- Inbound forwarder live: `hello@usepingback.com` → board's Gmail (via
  Namecheap eforward MX). Confirmed in MAK-173.
- Resend API key with full access — the prod key in `/opt/pingback/.env`
  is **send-only** and cannot manage domains. Board must either use the
  Resend dashboard for steps below, or generate a new full-access key
  scoped just for the Gmail send-as relay.

## Why apex re-verification is needed

Resend currently has only `updates.usepingback.com` verified (see
`deploy/RESEND_DNS_SETUP.md` and prior probes in MAK-156). To make Gmail
send as `hello@usepingback.com`, Resend must accept the apex as the
verified sending domain. That requires DKIM CNAMEs on the apex and an
SPF record that includes `_spf.resend.com`.

## Step 1 — Add apex in Resend dashboard

1. Sign in to https://resend.com (board's account).
2. **Domains → Add Domain → `usepingback.com`** (region `us-east-1`).
3. Resend will show three DKIM CNAME rows. Copy the host + target for
   each — they look like:
   - Host `resend._domainkey` → Target `<random>.dkim.amazonses.com` (or `.resend.com`)
   - Host `resend2._domainkey` → Target `<random>...`
   - Host `resend3._domainkey` → Target `<random>...`
4. Leave the dashboard open; come back to click **Verify** after step 2.

## Step 2 — Namecheap DNS

> **Important blocker discovered 2026-04-29:** Namecheap → Mail Settings
> mode is **Email Forwarding**. That mode (a) auto-manages the apex MX
> rows for `eforward1–5.registrar-servers.com`, (b) **locks the apex SPF
> TXT** so it can't be edited from Advanced DNS, and (c) **hides MX from
> the Advanced DNS dropdown**. To verify the apex in Resend we need both
> a merged apex SPF and a `send.usepingback.com` MX — neither is
> possible while in Email Forwarding mode. Path is to switch to
> **Custom MX**, replicate the eforward MX rows manually, then add the
> Resend rows.

Log in to Namecheap → **Domain List → Manage `usepingback.com` →
Advanced DNS**.

### 2a. Capture current apex MX values (so you can revert)

Already captured via `dig MX usepingback.com @dns1.registrar-servers.com`
on 2026-04-29:

| Priority | Mailserver                          |
| -------- | ----------------------------------- |
| 10       | `eforward1.registrar-servers.com.`  |
| 10       | `eforward2.registrar-servers.com.`  |
| 10       | `eforward3.registrar-servers.com.`  |
| 15       | `eforward4.registrar-servers.com.`  |
| 20       | `eforward5.registrar-servers.com.`  |

These five rows MUST be re-added under Custom MX immediately after the
mode switch, otherwise `hello@usepingback.com` inbound forwarding stops
working.

### 2b. Switch Mail Settings → Custom MX

In **Mail Settings** (the same screen as Email Forwarding), open the
mode dropdown and choose **Custom MX**. Save. The apex SPF row that
was previously locked becomes editable, and the **MX Record** option
appears in the Advanced DNS Add-Record dropdown.

### 2c. Re-add the 5 eforward MX rows under Host Records

Click **Add New Record** five times, with `Host = @` and the priority
+ value pairs from step 2a. Save All Changes. **Do not skip this step**
— missing these rows means inbound `hello@` mail starts bouncing
immediately.

### 2d. Merge the apex SPF (do NOT add a second TXT)

The existing apex SPF row should now be editable:

```
v=spf1 include:spf.efwd.registrar-servers.com ~all
```

Edit it in place to:

```
v=spf1 include:_spf.resend.com include:spf.efwd.registrar-servers.com ~all
```

A domain may have only one SPF TXT (RFC 7208) — adding a second silently
breaks both. Edit, don't append.

### 2e. Add the Resend records from step 1

Add the rows shown in the Resend dashboard. Modern Resend layout for a
freshly-added apex looks like:

| Type  | Host (Namecheap)        | Value (from Resend dashboard)              | TTL  |
| ----- | ----------------------- | ------------------------------------------ | ---- |
| TXT   | `resend._domainkey`     | `p=...` long DKIM public key               | Auto |
| MX    | `send` (priority 10)    | `feedback-smtp.us-east-1.amazonses.com.`   | Auto |
| TXT   | `send`                  | `v=spf1 include:amazonses.com ~all`        | Auto |

If your dashboard still shows the older "3 CNAMEs" format, use those
instead — Resend honours both. Either way, copy host/value verbatim
from the dashboard. Namecheap auto-appends `.usepingback.com`, so the
host field is just `resend._domainkey` / `send`, not the full FQDN.

### 2f. Smoke-test inbound BEFORE clicking Verify in Resend

After saving all rows, send a test email from any external account to
`hello@usepingback.com` and confirm it arrives in your gmail under the
`+hello_pingback` label (the existing forwarder route). If it does not
arrive within 2 minutes, **switch Mail Settings back to Email
Forwarding immediately** — that restores the auto-managed MX rows.
Then escalate; do not proceed to Resend verification.

### 2g. (Recommended) Tighten DMARC `rua`

Existing `_dmarc` is `v=DMARC1; p=none;` — works as-is. If you want
aggregate reports, change to:

```
v=DMARC1; p=none; rua=mailto:postmaster@usepingback.com
```

The `postmaster@` forwarder is already in place from MAK-95.

## Step 3 — Verify in Resend dashboard

Back to Resend → **Domains → `usepingback.com` → Verify**. Propagation
2–15 min usually, up to 24 h. All four rows (3 DKIM + SPF) must turn
green.

Smoke test by sending a test email through the dashboard "Send Test"
button to a third-party Gmail. Confirm SPF=PASS and DKIM=PASS in the
Gmail "Show original" view.

## Step 4 — Gmail "Send mail as"

In the board's Gmail: **Settings (gear) → See all settings → Accounts
and Import → Send mail as → Add another email address**.

| Field            | Value                              |
| ---------------- | ---------------------------------- |
| Name             | `Pingback` (or `hello`)            |
| Email address    | `hello@usepingback.com`            |
| Treat as alias   | leave checked                      |

Then **Next → SMTP server**:

| Field       | Value                               |
| ----------- | ----------------------------------- |
| SMTP Server | `smtp.resend.com`                   |
| Port        | `465`                               |
| Username    | `resend`                            |
| Password    | (Resend API key — see below)        |
| Connection  | **Secured connection using SSL**    |

For the password, paste a Resend API key. Either reuse the prod send-only
key from `/opt/pingback/.env` (`RESEND_API_KEY=re_...`) or — preferred —
generate a fresh "sending only" key in the Resend dashboard scoped just
for Gmail relay so it can be rotated independently.

## Step 5 — Verify the alias

Gmail sends a confirmation code to `hello@usepingback.com`. Because of
the inbound forwarder set up in MAK-173, that code arrives in the same
Gmail inbox tagged with `+hello_pingback`. Click the link or paste the
code into the Gmail dialog.

## Step 6 — Test outbound

1. Compose a new message in Gmail. The **From** dropdown now lists
   `hello@usepingback.com`.
2. Pick it; send to a third-party Gmail you control.
3. In the recipient inbox: **⋮ → Show original**. Confirm:
   - `From: hello@usepingback.com`
   - SPF: **PASS** with `smtp.resend.com`
   - DKIM: **PASS** with `d=usepingback.com`
   - DMARC: **PASS**
4. Screenshot the From dropdown + the show-original headers. Attach to
   MAK-175 to close it out.

## Troubleshooting

- **Resend says "domain not verified" after 30 min:** confirm CNAMEs
  resolve via `dig +short CNAME resend._domainkey.usepingback.com
  @8.8.8.8`. Empty response = Namecheap row missing or typo.
- **Gmail "couldn't reach smtp.resend.com":** wrong port. Use `465 +
  SSL`, not `587 + TLS` (Gmail's send-as form is picky).
- **Gmail "535 authentication failed":** username must be literal
  `resend` (lowercase), not the API key. Password is the API key.
- **Email lands in spam at recipient:** check DKIM signing domain — if
  Resend signs with `d=resend.com` instead of `d=usepingback.com`, the
  apex isn't yet verified server-side. Re-run **Verify** in Resend.

## Future cleanup

If we later move outbound app email off `updates.` and onto apex too,
update `pingback/config.py` `EMAIL_FROM_*` defaults and re-deploy.
Tracked separately, not in MAK-175 scope.
