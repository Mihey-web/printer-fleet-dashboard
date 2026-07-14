#!/usr/bin/env bash
# Installer script for systemd service on Debian/Raspberry Pi
# Usage: sudo ./install_service.sh /path/to/repo [user [group]]

set -euo pipefail

REPO_DIR=${1:-$(pwd)}
USER=${2:-${SUDO_USER:-$(whoami)}}
GROUP=${3:-$USER}
SERVICE_NAME=bambudiagnostic
SERVICE_FILE=deploy/${SERVICE_NAME}.service
TARGET=/etc/systemd/system/${SERVICE_NAME}.service

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Service template not found: $SERVICE_FILE"
  exit 1
fi

# Replace placeholders
TMP=$(mktemp)
sed "s|__USER__|${USER}|g; s|__GROUP__|${GROUP}|g; s|__WORKDIR__|${REPO_DIR}|g" "$SERVICE_FILE" > "$TMP"

# Copy to systemd
sudo cp "$TMP" "$TARGET"
rm "$TMP"

# Reload systemd and enable
sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE_NAME}.service

echo "Service installed and started. Use: sudo journalctl -u ${SERVICE_NAME}.service -f" 
