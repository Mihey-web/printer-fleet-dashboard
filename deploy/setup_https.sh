#!/usr/bin/env bash
# Setup HTTPS reverse proxy for BambuDiagnosticData on Raspberry Pi / Debian
# Usage: sudo ./deploy/setup_https.sh [LAN_IP]
#   LAN_IP — the Pi's LAN IP (e.g. 192.168.1.100). Auto-detected if omitted.
#
# After running, access the dashboard at https://<LAN_IP>
# Accept the self-signed certificate warning once per device.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root: sudo $0"
    exit 1
fi

LAN_IP="${1:-$(hostname -I | awk '{print $1}')}"

if [[ -z "$LAN_IP" ]]; then
    echo "Could not detect LAN IP. Provide it as an argument: sudo $0 192.168.1.100"
    exit 1
fi

echo "=== BambuDiagnosticData HTTPS setup ==="
echo "LAN IP: $LAN_IP"
echo ""

# 1. Install nginx
if ! command -v nginx &>/dev/null; then
    echo "Installing nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx
fi

# 2. Generate self-signed cert
echo "Generating self-signed SSL certificate for $LAN_IP..."
mkdir -p /etc/ssl/bambu
openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/ssl/bambu/privkey.pem \
    -out /etc/ssl/bambu/fullchain.pem \
    -subj "/CN=${LAN_IP}" \
    -addext "subjectAltName=IP:${LAN_IP}" 2>/dev/null

chmod 600 /etc/ssl/bambu/privkey.pem

# 3. Install nginx config
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NGINX_CONF="$SCRIPT_DIR/nginx-bambu.conf"

if [[ ! -f "$NGINX_CONF" ]]; then
    echo "nginx config not found: $NGINX_CONF"
    exit 1
fi

cp "$NGINX_CONF" /etc/nginx/sites-available/bambu

# Remove default site if it would conflict (it listens on port 80)
if [[ -f /etc/nginx/sites-enabled/default ]]; then
    rm /etc/nginx/sites-enabled/default
fi

# Enable our config if not already linked
if [[ ! -f /etc/nginx/sites-enabled/bambu ]]; then
    ln -s /etc/nginx/sites-available/bambu /etc/nginx/sites-enabled/
fi

# 4. Test and restart nginx
echo "Testing nginx config..."
nginx -t

echo "Restarting nginx..."
systemctl restart nginx

echo ""
echo "=== HTTPS setup complete! ==="
echo ""
echo "The old HTTP URL still works:  http://${LAN_IP}:8000"
echo "Use the new HTTPS URL:         https://${LAN_IP}"
echo ""
echo "For browser notifications to work:"
echo "  1. Open https://${LAN_IP} in your browser"
echo "  2. Click 'Advanced' → 'Proceed to ${LAN_IP} (unsafe)'"
echo "  3. Enable notifications in the dashboard settings"
echo ""
echo "Repeat steps 1-2 once on each of your 5 devices."
