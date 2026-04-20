#!/usr/bin/env bash
# EC2 Setup Script for Pingback (Amazon Linux 2023 / Ubuntu 22.04+)
# Run as root on a fresh t2.micro instance
set -euo pipefail

APP_DIR="/opt/pingback"
DATA_DIR="$APP_DIR/data"
APP_USER="pingback"

echo "=== Pingback EC2 Setup ==="

# Detect package manager and install prerequisites.
# App requires Python 3.10+ (PEP 604 union syntax). Amazon Linux 2023 ships 3.9
# as default, so on dnf we install python3.11 explicitly. Ubuntu 22.04 ships
# 3.10 which is fine.
PY_BIN=""
if command -v dnf &>/dev/null; then
    dnf update -y
    dnf install -y python3.11 python3.11-pip nginx certbot python3-certbot-nginx fail2ban git
    PY_BIN="python3.11"
elif command -v apt-get &>/dev/null; then
    apt-get update -y
    apt-get install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx fail2ban git
    PY_BIN="python3"
else
    echo "Unsupported package manager" && exit 1
fi

# Create app user
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

# Create directories
mkdir -p "$APP_DIR" "$DATA_DIR" /var/www/certbot
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Clone or copy app code
if [ -d "$APP_DIR/.git" ]; then
    echo "App directory already has code, skipping clone"
else
    echo ">>> Copy your app code to $APP_DIR (pingback/ directory + requirements.txt)"
    echo ">>> For example: scp -r ./pingback ./requirements.txt ec2-user@<IP>:/opt/pingback/"
fi

# Python virtual environment
"$PY_BIN" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/venv"

# Environment file
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/deploy/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo ">>> Edit $APP_DIR/.env with your actual values (ENCRYPTION_KEY, RESEND_API_KEY)"
fi

# Systemd service
cp "$APP_DIR/deploy/pingback.service" /etc/systemd/system/pingback.service
systemctl daemon-reload
systemctl enable pingback

# Nginx config — bootstrap HTTP-only. Swap to HTTPS via deploy/enable-https.sh after DNS is live.
cp "$APP_DIR/deploy/nginx-pingback-bootstrap.conf" /etc/nginx/conf.d/pingback.conf
# Remove default site if present
rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

# Host firewall (optional — AWS security groups gate ingress either way).
# Skip silently if the daemon is not running on this AMI (AL2023 ships with
# firewall-cmd installed but firewalld inactive).
if command -v ufw &>/dev/null; then
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
elif command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=ssh
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
else
    echo ">>> no host firewall configured — relying on AWS security group"
fi

# Fail2ban
systemctl enable fail2ban
systemctl start fail2ban

# Start nginx (without SSL initially — certbot will configure it)
nginx -t && systemctl enable nginx && systemctl restart nginx

# SQLite backup cron
cp "$APP_DIR/deploy/backup-db.sh" /opt/pingback/backup-db.sh
chmod +x /opt/pingback/backup-db.sh
(crontab -u "$APP_USER" -l 2>/dev/null || true; echo "0 */6 * * * /opt/pingback/backup-db.sh") | sort -u | crontab -u "$APP_USER" -

# CloudWatch log group retention + metric filters (MAK-60).
# Skipped automatically if the AWS CLI or instance-profile creds are missing —
# run `deploy/cloudwatch-setup.sh` manually from a host that has them.
if command -v aws &>/dev/null && aws sts get-caller-identity &>/dev/null; then
    bash "$APP_DIR/deploy/cloudwatch-setup.sh" || echo "!!! cloudwatch-setup.sh failed — re-run manually"
else
    echo ">>> skipping cloudwatch-setup.sh (no aws cli / creds). Run it from a host with logs:* permissions."
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/pingback/.env with actual credentials (ENCRYPTION_KEY, RESEND_API_KEY, etc.)"
echo "  2. Start the app: systemctl start pingback"
echo "  3. Verify: curl http://localhost:8000/health"
echo "  4. Point DNS A records for \$DOMAIN and www.\$DOMAIN to this EC2 public IP"
echo "  5. Enable HTTPS:"
echo "       DOMAIN=<your-apex-domain> CERTBOT_EMAIL=<ops-contact> \\"
echo "         bash /opt/pingback/deploy/enable-https.sh"
echo "  6. Verify HTTPS: curl https://<your-domain>/health"
echo "  7. (Optional) From an admin host, create CloudWatch alarms. See docs/OPERATIONS.md:"
echo "       PINGBACK_INSTANCE_ID=\$(curl -s http://169.254.169.254/latest/meta-data/instance-id) \\"
echo "         ALERT_EMAILS=\"ops@example.com\" bash deploy/cloudwatch-alarms.sh"
