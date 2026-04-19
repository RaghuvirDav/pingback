#!/usr/bin/env bash
# EC2 Setup Script for Pingback (Amazon Linux 2023 / Ubuntu 22.04+)
# Run as root on a fresh t2.micro instance
set -euo pipefail

APP_DIR="/opt/pingback"
DATA_DIR="$APP_DIR/data"
APP_USER="pingback"

echo "=== Pingback EC2 Setup ==="

# Detect package manager
if command -v dnf &>/dev/null; then
    PKG="dnf"
    $PKG update -y
    $PKG install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx fail2ban git
elif command -v apt-get &>/dev/null; then
    PKG="apt-get"
    $PKG update -y
    $PKG install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx fail2ban git
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
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

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

# Nginx config
cp "$APP_DIR/deploy/nginx-pingback.conf" /etc/nginx/conf.d/pingback.conf
# Remove default site if present
rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

# Firewall (ufw for Ubuntu, firewalld for AL2023)
if command -v ufw &>/dev/null; then
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-service=ssh
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
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

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/pingback/.env with actual credentials"
echo "  2. Start the app: systemctl start pingback"
echo "  3. Verify: curl http://localhost:8000/health"
echo "  4. Point DNS A record for usepingback.com to this EC2 public IP"
echo "  5. Run certbot: certbot --nginx -d usepingback.com -d www.usepingback.com"
echo "  6. Verify HTTPS: curl https://usepingback.com/health"
