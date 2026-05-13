#!/usr/bin/env bash
set -euo pipefail

UNIT_FILE="${HOME}/.config/systemd/user/manager-tt-launcher.service"

systemctl --user disable --now manager-tt-launcher.service 2>/dev/null || true
rm -f "$UNIT_FILE"
systemctl --user daemon-reload

echo "[launcher] Removed user service: manager-tt-launcher.service"
