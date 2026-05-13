#!/usr/bin/env bash
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_FILE="${UNIT_DIR}/manager-tt-launcher.service"

mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Manager TT Launcher Backend
After=default.target network.target

[Service]
Type=simple
WorkingDirectory=${LAUNCHER_DIR}
ExecStart=/usr/bin/python3 ${LAUNCHER_DIR}/launcher_server.py --port 58080
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now manager-tt-launcher.service

echo "[launcher] Installed and started user service: manager-tt-launcher.service"
echo "[launcher] Check status: systemctl --user status manager-tt-launcher.service"
