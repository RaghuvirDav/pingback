# Pingback — Self-Hosting Guide (v1.0)

This guide walks you through deploying Pingback to your own AWS free-tier account on a `t3.micro` EC2 instance, with HTTPS, error tracking, and backups. Expected time: 30–45 minutes once DNS is ready.

## 0. What you need before you start

- An AWS account (free tier is sufficient).
- A domain you control (for HTTPS via Let's Encrypt). Apex + `www` both recommended.
- A Resend account (https://resend.com) for transactional email — free tier is fine.
- Optional: a Sentry account (https://sentry.io) for error tracking.
- Optional: a Stripe account if you want paid billing tiers.
- An SSH client and local `git`.

No specific AWS region is required. Examples below assume `us-east-1`; swap in whatever region you prefer.

## 1. Launch an EC2 instance

1. In the AWS Console → **EC2 → Launch instance**.
2. Name: `pingback`.
3. AMI: **Amazon Linux 2023** or **Ubuntu 22.04 LTS**.
4. Instance type: `t3.micro` (free tier on accounts created after July 2023; `t2.micro` is no longer free-tier eligible in new accounts).
5. Key pair: create a new one; download the `.pem`.
6. Network:
   - Security group: allow `22` (SSH, your IP), `80` (HTTP, anywhere), `443` (HTTPS, anywhere).
7. Storage: default 8 GB gp3 is enough to start.
8. Launch.

Note the instance's public IPv4 address.

## 2. Point DNS at the instance

In your DNS provider, create two A records pointing at the EC2 public IP:

| Host             | Type | Value                 |
|------------------|------|-----------------------|
| `your-domain.com`| A    | `<EC2 public IPv4>`   |
| `www`            | A    | `<EC2 public IPv4>`   |

DNS propagation can take a few minutes to an hour. Verify with `dig +short your-domain.com` before continuing to step 5.

## 3. Bootstrap the host

SSH in and clone the repo:

```bash
ssh -i /path/to/your-key.pem ec2-user@<EC2_IP>          # Amazon Linux
# or
ssh -i /path/to/your-key.pem ubuntu@<EC2_IP>            # Ubuntu

sudo dnf install -y git || sudo apt-get install -y git
sudo git clone https://github.com/<your-fork>/pingback.git /opt/pingback
cd /opt/pingback
sudo bash deploy/setup-ec2.sh
```

`setup-ec2.sh` installs Python, nginx, certbot, fail2ban, creates the `pingback` system user, sets up the venv + systemd unit, and installs an HTTP-only nginx bootstrap config.

## 4. Fill in `/opt/pingback/.env`

The bootstrap script copies `deploy/.env.example` to `/opt/pingback/.env` if it doesn't exist. Edit it:

```bash
sudo -e /opt/pingback/.env
```

At minimum you must fill:

- `APP_BASE_URL=https://your-domain.com`
- `ENCRYPTION_KEY=<Fernet key>` — generate with:
  ```bash
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `RESEND_API_KEY=<your Resend key>`
- `RESEND_FROM_EMAIL=Pingback <noreply@your-domain.com>` (the domain must be verified in Resend)

Optional blocks (Sentry, Stripe, AWS region) are documented inline in `deploy/.env.example`. Leave them blank to disable those integrations.

Start the service:

```bash
sudo systemctl start pingback
curl http://localhost:8000/health   # should return {"status":"ok"}
```

## 5. Enable HTTPS

Once DNS is pointing at your instance (step 2), run:

```bash
sudo DOMAIN=your-domain.com CERTBOT_EMAIL=ops@your-domain.com \
  bash /opt/pingback/deploy/enable-https.sh
```

This script:

1. Verifies DNS resolves to the instance's public IP.
2. Installs certbot (if missing) and requests a Let's Encrypt cert for `your-domain.com` + `www.your-domain.com`.
3. Renders `deploy/nginx-pingback.conf.template` with your domain and installs it as `/etc/nginx/conf.d/pingback.conf`.
4. Reloads nginx and verifies `https://your-domain.com/health` returns 200.
5. Enables the `certbot-renew.timer` for automatic renewal.

## 6. (Optional) CloudWatch alarms

If you want CPU / disk / status-check alarms with email notifications:

```bash
# From an admin host with an IAM role that has cloudwatch:PutMetricAlarm + sns:CreateTopic
export PINGBACK_INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
export ALERT_EMAILS="ops@your-domain.com"
bash /opt/pingback/deploy/cloudwatch-alarms.sh
```

Full details in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## 7. Verify

- `curl https://your-domain.com/health` → `{"status":"ok"}`
- Open `https://your-domain.com/` in a browser — you should see the landing page.
- Sign up for a local account, create a monitor pointing at `https://example.com`, and wait one check interval (default 5 min). The monitor should flip to green.
- `sudo journalctl -u pingback -n 50 --no-pager` — tail app logs.

## 7a. Deploys and rollback (Phase 1 — near-zero-downtime)

Pingback uses a versioned-release layout so production updates do not require operator intervention to roll back. Each deploy lands in its own directory under `/opt/pingback/releases/<git-sha>/` and `/opt/pingback/current` is an atomic symlink to the active release.

**Phase 1 trade-off.** Single-process gunicorn cannot pick up new code on `systemctl reload` (SIGHUP) — its master cwd is resolved through the `current` symlink at service-start time and is frozen to that inode. Worker forks inherit the frozen cwd, so SIGHUP-spawned workers re-import the OLD release. We therefore use `systemctl restart`, which re-execs gunicorn against the new symlink target. Cost: a ~2-4 second window during which new connections see 502; nginx `proxy_next_upstream` (twin-entry upstream + 5s tries timeout) catches in-flight requests that complete inside the window. Measured failure rate at 8 RPS during a deploy: ~0.8% (3/380). Rollback: 0% over 30s sample because the rollback path skips unpack/pip/preflight.

Phase 2 ([MAK-180](#)) lifts this to true zero-downtime with two systemd units on different ports + nginx blue/green flip.

**Layout**

```
/opt/pingback/
├── current -> releases/<sha>/        # atomic symlink, swapped on deploy
├── releases/
│   ├── <sha-N>/                      # current release (code + venv)
│   ├── <sha-N-1>/                    # previous release (rollback target)
│   └── ...
├── .env                              # shared, root:pingback 640
└── data/pingback.db                  # shared, lives outside release dirs
```

**Deploy a new build**

From your laptop, build a release tarball and copy it to the box:

```bash
SHA=$(git rev-parse --short HEAD)
git archive --format=tar.gz -o /tmp/pingback-$SHA.tar.gz HEAD
scp -i .ssh/pingback-ec2.pem /tmp/pingback-$SHA.tar.gz ec2-user@<host>:/tmp/
ssh -i .ssh/pingback-ec2.pem ec2-user@<host> \
  "sudo /opt/pingback/current/deploy/release.sh /tmp/pingback-$SHA.tar.gz $SHA"
```

`release.sh` unpacks the tarball into `/opt/pingback/releases/<sha>/`, builds a per-release venv (seeded from the previous release with hardlinks so unchanged wheels are not re-downloaded), runs an in-process import preflight, swaps the `current` symlink atomically, and reloads systemd. It then polls `/healthz` for up to 30 seconds and verifies the running version flipped to the new sha. **If the health check fails, the symlink is automatically reverted to the previous release.**

Health check contract:

```bash
curl -s http://127.0.0.1:8000/healthz
# {"ok":true,"version":"<sha>"}
curl -sI https://<host>/ | grep -i x-pingback-version
# X-Pingback-Version: <sha>
```

**Roll back**

One command, completes in well under 5 seconds:

```bash
ssh -i .ssh/pingback-ec2.pem ec2-user@<host> "sudo /opt/pingback/current/deploy/rollback.sh"
```

By default the script reads `/opt/pingback/releases/.previous` (written by `release.sh` on every deploy) and points `current` at that release. To roll to a specific older release, pass its sha: `sudo deploy/rollback.sh <sha>`.

**Acceptance test (near-zero 502s under load)**

From a workstation with `hey` installed, drive 10 RPS at `/healthz` for 60 seconds while a deploy runs in another shell. The deploy window typically reports ~1% failed requests (the gunicorn restart gap); rollback reports 0%.

```bash
hey -z 60s -c 10 -q 1 https://<host>/healthz
```

**Migrating an existing host from the legacy layout**

If `/opt/pingback` is the pre-MAK-179 flat layout (code at `/opt/pingback/pingback/`, venv at `/opt/pingback/venv/`), do the cut-over once:

```bash
SHA=$(git rev-parse --short HEAD)
sudo mkdir -p /opt/pingback/releases/$SHA
sudo cp -a /opt/pingback/{pingback,deploy,requirements.txt,scripts} /opt/pingback/releases/$SHA/
sudo cp -a /opt/pingback/venv /opt/pingback/releases/$SHA/venv
sudo ln -s /opt/pingback/.env /opt/pingback/releases/$SHA/.env
sudo ln -s /opt/pingback/data /opt/pingback/releases/$SHA/data
echo "$SHA" | sudo tee /opt/pingback/releases/$SHA/RELEASE_SHA
sudo chown -R pingback:pingback /opt/pingback/releases/$SHA
sudo ln -sfn /opt/pingback/releases/$SHA /opt/pingback/current
sudo cp /opt/pingback/current/deploy/pingback.service /etc/systemd/system/pingback.service
sudo systemctl daemon-reload
sudo systemctl restart pingback
```

After the first migration, every subsequent deploy is one `release.sh` invocation.

## 8. Backups

`setup-ec2.sh` installs an hourly SQLite backup cron for the `pingback` user (`deploy/backup-db.sh`). Backups land in `/opt/pingback/data/backups/`. If you want offsite copies, add an `aws s3 sync` step in that script or attach an S3 bucket via IAM role.

## 9. Billing (optional — Paddle)

Pingback ships a Free / Pro tier model wired to **Paddle** (Merchant of Record — they handle global VAT/GST and the payment-method UI). The integration is **off by default** — leave the `PADDLE_*` env vars blank and Pingback runs as a single-tier free product.

Why Paddle instead of Stripe? Paddle is available in countries (notably India) where Stripe is invite-only, and as a MoR they handle tax compliance for you. Trade-off: 5% + $0.50 per transaction vs Stripe's 2.9% + 30¢. See [MAK-85](https://github.com/RaghuvirDav/pingback/issues) for the full decision write-up.

To turn it on you need a Paddle vendor account, a Pro product, and one or more recurring prices.

Required env vars (in `/opt/pingback/.env`):

| Variable                       | Where it comes from                                                                          |
|--------------------------------|----------------------------------------------------------------------------------------------|
| `PADDLE_ENVIRONMENT`           | `sandbox` for dev, `production` for live (drives the API base URL + Paddle.js env)            |
| `PADDLE_API_KEY`               | Paddle Dashboard → Developer Tools → Authentication (starts with `pdl_live_apikey_` or `pdl_sdbx_apikey_`) |
| `PADDLE_CLIENT_TOKEN`          | Same page (starts with `live_` or `test_`) — safe to expose in client JS                      |
| `PADDLE_WEBHOOK_SECRET`        | Notification settings page → secret key for the endpoint (starts with `pdl_ntfset_`)         |
| `PADDLE_PRODUCT_ID`            | Catalog → Products → your Pro product (starts with `pro_`)                                   |
| `PADDLE_PRICE_ID_MONTHLY`      | Same product → recurring monthly price (starts with `pri_`)                                  |
| `PADDLE_PRICE_ID_YEARLY`       | Optional — recurring yearly price                                                             |
| `PADDLE_DISCOUNT_ID_LAUNCH`    | Optional — promo code to auto-apply at checkout (starts with `dsc_`)                          |

Webhook endpoint to register in Paddle (Notifications → Add destination):

- URL: `https://your-domain.com/api/paddle/webhook`
- Events to send:
  - `subscription.created`
  - `subscription.updated`
  - `subscription.canceled`
  - `transaction.completed`
  - `transaction.payment_failed`

After saving the endpoint, copy its secret key into `PADDLE_WEBHOOK_SECRET` and `sudo systemctl restart pingback`.

### Checkout flow

Pingback uses **Paddle.js overlay** (client-side) — the `Upgrade to Pro` button opens a modal on the same page, no redirect. There is no server-side `/billing/checkout` endpoint; the webhook is the only authority for plan state.

The `customData.pingback_user_id` field is sent through Paddle so the very first `subscription.created` webhook can attach the new Paddle customer to the right local user without a separate handshake.

### What the plans actually enforce (server-side)

| Limit              | Free                | Pro                          |
|--------------------|---------------------|------------------------------|
| Monitors           | 5                   | Unlimited                    |
| Min check interval | 5 minutes (300s)    | 1 minute (60s)               |
| History retention  | 7 days              | 90 days                      |

Limits are enforced by the API and dashboard routes, not just the UI — a client cannot edit forms to cheat past them. See `pingback/services/plans.py` for the source of truth.

### Sandbox walkthrough (recommended before going live)

1. Sign up at https://sandbox-vendors.paddle.com — separate from production, no identity verification needed.
2. Create a Pro product and one or more recurring prices; copy the IDs into `.env`.
3. Add a notification destination pointing at `https://your-domain.com/api/paddle/webhook` and copy the secret into `PADDLE_WEBHOOK_SECRET`.
4. Set `PADDLE_ENVIRONMENT=sandbox` and restart: `sudo systemctl restart pingback`.
5. Sign up a Pingback user, click `Upgrade to Pro`, complete the overlay with Paddle's sandbox test card (`4242 4242 4242 4242`, any future date, any CVV).
6. Verify the row in `users` flipped to `plan='pro'` and that `paddle_customer_id` + `paddle_subscription_id` populated.
7. Open the customer portal (`Manage subscription`) and cancel — confirm `plan_cancel_at` is set and the row stays on Pro until that date.

Webhook deliveries are idempotent by Paddle event id — Paddle's at-least-once retries are recorded in the `paddle_events` table and ignored on the second hit.

### Going to production

Swap to your production Paddle keys, set `PADDLE_ENVIRONMENT=production`, and re-register the webhook destination from the production dashboard (it has a different secret). Production checkout requires identity verification to be cleared on the Paddle account first — keys can be in the file before that, you just won't take real money until verification lands.

## Troubleshooting

| Symptom                                      | Likely cause                                        | Fix                                                                 |
|----------------------------------------------|-----------------------------------------------------|---------------------------------------------------------------------|
| `enable-https.sh` fails at DNS check         | A record not propagated yet                          | `dig +short your-domain.com` — wait until it returns the EC2 IP     |
| certbot "too many requests"                  | Let's Encrypt rate limit hit (retrying staging)      | Wait 1h, or pass `--staging` flag manually                           |
| `502 Bad Gateway` from nginx                  | `pingback` systemd unit not running                  | `sudo systemctl status pingback` → `journalctl -u pingback`          |
| Emails not sending                            | Resend sender domain not verified                    | Verify your domain in the Resend dashboard                           |
| `health` endpoint timing out                  | Security group missing 80/443                        | Add inbound rules in the EC2 console                                 |

## Uninstalling

```bash
sudo systemctl stop pingback
sudo systemctl disable pingback
sudo rm -rf /opt/pingback /etc/systemd/system/pingback.service
sudo rm -f /etc/nginx/conf.d/pingback.conf
sudo systemctl reload nginx
```

Then terminate the EC2 instance and delete the DNS records.
