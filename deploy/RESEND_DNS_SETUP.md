# Resend + DNS Setup (usepingback.com)

One-time checklist for wiring outbound transactional email. Covers account
signup, domain verification, sender identities, and the prod key drop. Ticket:
[MAK-95](/MAK/issues/MAK-95).

## 1. Create the Resend account

1. Sign up at https://resend.com using the board's email (or `hello@usepingback.com` once that inbox exists).
2. Create a workspace named `Pingback`.
3. From **API Keys → Create API Key**, generate a full-access key named
   `pingback-prod`. Copy the value immediately — Resend only shows it once.

## 2. Add the domain

In Resend → **Domains → Add Domain**, add `usepingback.com` with region `us-east-1`.
Resend will show four DNS records. Paste them into the registrar's DNS panel
(the domain is registered through the board; currently Cloudflare / GoDaddy —
verify which before editing).

| Type  | Host                                | Value / Target                                                  | TTL  |
| ----- | ----------------------------------- | ---------------------------------------------------------------- | ---- |
| TXT   | `usepingback.com`                    | `v=spf1 include:_spf.resend.com ~all`                            | Auto |
| CNAME | `resend._domainkey.usepingback.com` | (shown in Resend dashboard, ends in `.resend.com`)               | Auto |
| CNAME | `resend2._domainkey.usepingback.com`| (shown in Resend dashboard)                                      | Auto |
| CNAME | `resend3._domainkey.usepingback.com`| (shown in Resend dashboard)                                      | Auto |
| TXT   | `_dmarc.usepingback.com`             | `v=DMARC1; p=none; rua=mailto:postmaster@usepingback.com`        | Auto |

Glossary for the board:

- **SPF** (Sender Policy Framework) — tells receiving inboxes "Resend is allowed to send for this domain."
- **DKIM** (DomainKeys Identified Mail) — signs every outgoing message so it can't be tampered with.
- **DMARC** (Domain-based Message Authentication, Reporting, and Conformance) — the policy layer on top of SPF/DKIM. Starting at `p=none` (monitor only) so we can watch reports before enforcing. Tighten to `p=quarantine` after ~2 weeks of clean reports.

Click **Verify** in Resend. DNS propagation is usually 2–15 minutes but can
take up to 24 h.

## 3. Configure sender identities

Sender addresses used by the app (see `pingback/config.py` → `EMAIL_FROM_*`):

- `digest@usepingback.com` — daily digest (`EMAIL_FROM_DIGEST`).
- `noreply@usepingback.com` — email verification, password reset, billing receipts (`EMAIL_FROM_NOREPLY`).

Do **not** send from `hello@usepingback.com`. `hello@` is reserved for
human-to-human conversation so customer replies land in a real inbox.

No extra configuration inside Resend is required per sender — any `*@usepingback.com`
will work once the domain is verified.

## 4. Drop the key into prod

The prod `.env` lives at `/opt/pingback/.env` (root-owned, 600). Deploy
process is `scp → sudo cp → sudo systemctl restart pingback` — there is no
git checkout on the prod host. Append / update:

```ini
RESEND_API_KEY=<paste the key from step 1>
EMAIL_FROM_DIGEST=Pingback Digest <digest@usepingback.com>
EMAIL_FROM_NOREPLY=Pingback <noreply@usepingback.com>
```

Then:

```bash
sudo systemctl restart pingback
journalctl -u pingback -n 50
```

## 5. Smoke test

From the EC2 host (Resend key never touches a laptop):

```bash
cd /opt/pingback
source .venv/bin/activate
python - <<'PY'
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
- Add `postmaster@usepingback.com` forwarding so aggregate reports reach a human inbox.
- Revisit SES migration once sustained volume passes ~2,500 emails/month
  (the Resend free tier is 3,000/mo — see [MAK-34](/MAK/issues/MAK-34)).
