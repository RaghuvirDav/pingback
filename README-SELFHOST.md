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

## 8. Backups

`setup-ec2.sh` installs an hourly SQLite backup cron for the `pingback` user (`deploy/backup-db.sh`). Backups land in `/opt/pingback/data/backups/`. If you want offsite copies, add an `aws s3 sync` step in that script or attach an S3 bucket via IAM role.

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
