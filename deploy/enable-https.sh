#!/usr/bin/env bash
# Run AFTER DNS A records for usepingback.com + www.usepingback.com point at this host.
# Provisions Let's Encrypt certs and swaps nginx to the full HTTPS template.
set -euo pipefail

DOMAIN="usepingback.com"
WWW="www.${DOMAIN}"
EMAIL="${CERTBOT_EMAIL:-board@usepingback.com}"

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
sudo dnf install -y certbot python3-certbot-nginx

echo ">>> Requesting cert..."
sudo certbot --nginx \
    --non-interactive \
    --agree-tos \
    --email "$EMAIL" \
    --redirect \
    -d "$DOMAIN" -d "$WWW"

echo ">>> Swapping nginx to the full HTTPS template..."
sudo cp /opt/pingback/deploy/nginx-pingback.conf /etc/nginx/conf.d/pingback.conf
sudo nginx -t
sudo systemctl reload nginx

echo ">>> Verifying HTTPS..."
curl -sS "https://${DOMAIN}/health" && echo
curl -sS "https://${WWW}/health" && echo

echo ">>> Cert auto-renew timer:"
sudo systemctl list-timers | grep -i certbot || true

echo "=== HTTPS enabled ==="
