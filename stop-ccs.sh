#!/usr/bin/env bash
set -euo pipefail

CCS_DIR="/home/yun/桌面/workspace/cc-switch"
DEV_PORT=3001

echo "[ccs] Stopping CC Switch..."

pkill -f "$CCS_DIR" 2>/dev/null || true
pkill -f "pnpm exec vite --port $DEV_PORT --strictPort" 2>/dev/null || true
pkill -f "tauri dev -c" 2>/dev/null || true
pkill -f "cargo run --no-default-features --color always --" 2>/dev/null || true

PIDS="$(lsof -ti TCP:$DEV_PORT 2>/dev/null || true)"
if [[ -n "$PIDS" ]]; then
  echo "$PIDS" | xargs -r kill 2>/dev/null || true
  sleep 1
  echo "$PIDS" | xargs -r kill -9 2>/dev/null || true
fi

echo "[ccs] Stopped."
