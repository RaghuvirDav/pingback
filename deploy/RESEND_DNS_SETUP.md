# Resend + DNS Setup (usepingback.com)

One-time checklist for wiring outbound transactional email. Covers account
signup, domain verification, sender identities, and the prod key drop.
Ticket: [MAK-95](/MAK/issues/MAK-95).

## 1. Create the Resend account

1. Sign up at https://resend.com using `davraghuvir9+resend_pingback@gmail.com`
   (gmail subaddressing — all mail still lands in your inbox but gmail can
   filter/label it as Resend traffic, matching the `+hello_pingback` alias we
   used for the Namecheap inbound forwarder on [MAK-92](/MAK/issues/MAK-92)).
2. Create a workspace named `Pingback`.
3. From **API Keys → Create API Key**, generate a full-access key named
   `pingback-prod`. Copy the value immediately — Resend only shows it once.

## 2. DNS + postmaster forwarder (Namecheap)

DNS is on Namecheap — the domain's nameservers are `dns1.registrar-servers.com`
/ `dns2.registrar-servers.com`. Log in to **Namecheap → Domain List → Manage
usepingback.com → Advanced DNS**.

In Resend → **Domains → Add Domain**, add `usepingback.com` (region
`us-east-1`). Resend shows four records; you will also need to merge one
existing record and add two Namecheap email forwarders.

### 2a. Merge the SPF record (do NOT add a second row)

There is already an SPF TXT record from the Namecheap forwarder for
`hello@usepingback.com`:

```
v=spf1 include:spf.efwd.registrar-servers.com ~all
```

Per **RFC 7208 a domain may have exactly ONE SPF TXT record.** Adding a
second row silently breaks both (receivers see a `permerror` and reject
everything). **Edit the existing TXT in place** and change the value to:

```
v=spf1 include:_spf.resend.com include:spf.efwd.registrar-servers.com ~all
```

This authorises Resend for outbound AND keeps Namecheap forwarding working
for inbound `hello@` replies.

### 2b. Add DKIM + DMARC

From Resend's dashboard, add these rows in Namecheap Advanced DNS:

| Record Type | Host                   | Value / Target                                               | TTL  |
| ----------- | ---------------------- | ------------------------------------------------------------ | ---- |
| CNAME       | `resend._domainkey`    | (shown in Resend dashboard, ends in `.resend.com`)           | Auto |
| CNAME       | `resend2._domainkey`   | (shown in Resend dashboard)                                  | Auto |
| CNAME       | `resend3._domainkey`   | (shown in Resend dashboard)                                  | Auto |
| TXT         | `_dmarc`               | `v=DMARC1; p=none; rua=mailto:postmaster@usepingback.com`    | Auto |

Namecheap auto-appends the domain to the Host field — enter `_dmarc`, not
`_dmarc.usepingback.com`.

### 2c. Set up postmaster + abuse forwarders BEFORE enabling DMARC

DMARC publishes `rua=mailto:postmaster@usepingback.com`, which causes every
major inbox provider to mail daily aggregate reports there. If that mailbox
doesn't resolve, reports bounce and receivers start distrusting the domain.

In Namecheap → **Domain List → Manage usepingback.com → Domain tab → Redirect
Email** (same screen as the `hello@` forwarder from MAK-92), add:

- `postmaster@usepingback.com` → `davraghuvir9+postmaster_pingback@gmail.com`
- `abuse@usepingback.com`      → `davraghuvir9+abuse_pingback@gmail.com`

Do this in the same sitting as the DNS rows above so the chain is intact
from the moment Resend starts signing messages.

### 2d. Verify

Back in Resend → **Domains**, click **Verify**. Propagation is usually
2–15 minutes but can take up to 24 h. All four rows should turn green.

### Glossary (for the board)

- **SPF** (Sender Policy Framework) — tells receiving inboxes "Resend is allowed to send for this domain."
- **DKIM** (DomainKeys Identified Mail) — signs every outgoing message so it can't be tampered with.
- **DMARC** (Domain-based Message Authentication, Reporting, and Conformance) — the policy layer on top of SPF/DKIM. Starting at `p=none` (monitor only) so we can watch reports before enforcing. Tighten to `p=quarantine` after ~2 weeks of clean reports.

## 3. Sender identities

Addresses used by the app (see `pingback/config.py` → `EMAIL_FROM_*`):

- `daily_status@usepingback.com` — daily digest (`EMAIL_FROM_DAILY_STATUS`).
- `noreply@usepingback.com` — email verification, password reset, billing receipts (`EMAIL_FROM_NOREPLY`).
- `hello@usepingback.com` — **inbound only**, forwarded to the board's gmail via Namecheap. Never set as an outbound sender.

No extra configuration inside Resend is required per sender — any
`*@usepingback.com` address will work once the domain is verified.

## 4. Drop the key into prod

`/opt/pingback/.env` on the EC2 host is owned by `pingback:pingback`, mode
`600`. Deploys are `scp → sudo cp → sudo systemctl restart pingback` —
there is no git checkout on the prod host. Append / update:

```ini
RESEND_API_KEY=<paste the key from step 1>
EMAIL_FROM_DAILY_STATUS=Pingback Daily Status <daily_status@usepingback.com>
EMAIL_FROM_NOREPLY=Pingback <noreply@usepingback.com>
```

Then reload and tail the journal:

```bash
sudo systemctl restart pingback
journalctl -u pingback -n 50
```

## 5. Smoke test

From the EC2 host (the Resend key never touches a laptop). Run as the
`pingback` service user and use the real venv at `/opt/pingback/venv/`:

```bash
sudo -u pingback /opt/pingback/venv/bin/python - <<'PY'
from pingback.services.email import send_email
msg_id = send_email(
    to="davraghuvir9@gmail.com",
    subject="pingback email pipeline live",
    text="If you are reading this, Resend + DNS + systemd are all wired correctly.",
)
print("sent:", msg_id)
PY
```

Paste the returned `msg_id` and the Resend delivery receipt into
[MAK-95](/MAK/issues/MAK-95) to close it out.

## 6. Follow-ups (after 2 weeks clean)

- Tighten DMARC: `p=quarantine; pct=25` → `p=reject` over two rollout steps.
- Revisit SES migration once sustained volume passes ~2,500 emails/month
  (the Resend free tier is 3,000/mo — see [MAK-34](/MAK/issues/MAK-34)).
