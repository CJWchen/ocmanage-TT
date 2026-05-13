#!/usr/bin/env bash
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=58080
SERVICE="manager-tt-launcher.service"
TARGET_PAGE="${1:-launcher.html}"

if ! systemctl --user is-enabled "$SERVICE" >/dev/null 2>&1; then
  echo "[launcher] User service not installed. Installing..."
  bash "$LAUNCHER_DIR/install_launcher_service.sh"
elif ! systemctl --user is-active "$SERVICE" >/dev/null 2>&1; then
  echo "[launcher] User service is not running. Starting..."
  systemctl --user start "$SERVICE"
fi

for i in {1..20}; do
  if curl -sf "http://127.0.0.1:$PORT/cgi-bin/status" >/dev/null 2>&1; then
    echo "[launcher] Backend ready on http://127.0.0.1:$PORT"
    break
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:$PORT/cgi-bin/status" >/dev/null 2>&1; then
  echo "[launcher] ERROR: Backend not reachable on port $PORT"
  exit 1
fi

TARGET_PATH="$LAUNCHER_DIR/$TARGET_PAGE"
if [ ! -f "$TARGET_PATH" ]; then
  echo "[launcher] ERROR: Page not found: $TARGET_PATH"
  exit 1
fi

if command -v xdg-open &>/dev/null; then
  xdg-open "$TARGET_PATH" 2>/dev/null || true
elif command -v gnome-open &>/dev/null; then
  gnome-open "$TARGET_PATH" 2>/dev/null || true
else
  echo "[launcher] Open the file manually: $TARGET_PATH"
fi

echo "[launcher] Opened $TARGET_PAGE. Backend is managed by systemd user service: $SERVICE"
