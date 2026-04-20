#!/usr/bin/env bash
# Provisions Let's Encrypt certs and swaps nginx to the full HTTPS template.
# Run AFTER DNS A records for $DOMAIN and www.$DOMAIN point at this host.
#
# Usage:
#   DOMAIN=example.com CERTBOT_EMAIL=ops@example.com bash deploy/enable-https.sh
#
# Required env:
#   DOMAIN         apex domain for the deployment (e.g. example.com)
#   CERTBOT_EMAIL  contact address Let's Encrypt uses for expiry notices
#
# Optional:
#   APP_DIR        defaults to /opt/pingback
set -euo pipefail

: "${DOMAIN:?Set DOMAIN=<your-apex-domain> before running (e.g. example.com)}"
: "${CERTBOT_EMAIL:?Set CERTBOT_EMAIL=<ops-contact> for Let's Encrypt notices}"

APP_DIR="${APP_DIR:-/opt/pingback}"
WWW="www.${DOMAIN}"
NGINX_TEMPLATE="$APP_DIR/deploy/nginx-pingback.conf.template"
NGINX_TARGET="/etc/nginx/conf.d/pingback.conf"

EXPECTED_IP="$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/public-ipv4 || true)"
echo "Expected public IP: ${EXPECTED_IP:-unknown}"

echo ">>> Verifying DNS points here..."
for host in "$DOMAIN" "$WWW"; do
    ACTUAL=$(dig +short A "$host" | tail -n1)
    if [ -z "$ACTUAL" ]; then
        echo "  $host → (no A record yet)"; exit 1
    fi
    echo "  $host → $ACTUAL"
    if [ -n "$EXPECTED_IP" ] && [ "$ACTUAL" != "$EXPECTED_IP" ]; then
        echo "!!! $host points at $ACTUAL, expected $EXPECTED_IP"
        echo "!!! Wait for DNS propagation before running certbot"
        exit 1
    fi
done

echo ">>> Installing certbot..."
if command -v dnf &>/dev/null; then
    sudo dnf install -y certbot python3-certbot-nginx
elif command -v apt-get &>/dev/null; then
    sudo apt-get update -y
    sudo apt-get install -y certbot python3-certbot-nginx
else
    echo "!!! Unsupported package manager — install certbot manually" >&2
    exit 1
fi

echo ">>> Requesting cert..."
sudo certbot --nginx \
    --non-interactive \
    --agree-tos \
    --email "$CERTBOT_EMAIL" \
    --redirect \
    -d "$DOMAIN" -d "$WWW"

echo ">>> Rendering nginx HTTPS template for $DOMAIN..."
if [ ! -f "$NGINX_TEMPLATE" ]; then
    echo "!!! Missing template: $NGINX_TEMPLATE" >&2
    exit 1
fi
sudo bash -c "sed 's/__DOMAIN__/${DOMAIN}/g' '$NGINX_TEMPLATE' > '$NGINX_TARGET'"
sudo nginx -t
sudo systemctl reload nginx

echo ">>> Verifying HTTPS..."
curl -sS "https://${DOMAIN}/health" && echo
curl -sS "https://${WWW}/health" && echo

echo ">>> Enabling cert auto-renew timer..."
sudo systemctl enable --now certbot-renew.timer
sudo systemctl list-timers certbot-renew.timer --all

echo "=== HTTPS enabled for ${DOMAIN} ==="
