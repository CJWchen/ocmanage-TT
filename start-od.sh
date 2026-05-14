#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
OD="$WORKSPACE_DIR/open-design"
LOG="/tmp/od-all.log"

# Cleanup background processes on exit
BG_PIDS=()
cleanup() {
  for pid in "${BG_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

echo "[od] Starting Open Design..."
> "$LOG"

# Step 1: daemon (必须最先启动)
echo "[od] Starting daemon (port 7456)..." | tee -a "$LOG"
cd "$OD"
nohup pnpm --filter @open-design/daemon dev >> "$LOG" 2>&1 &
BG_PIDS+=($!)
for i in {1..45}; do
  if ss -tlnp "sport = :7456" 2>/dev/null | grep -q LISTEN; then
    echo "[od] Daemon ready (port 7456)" | tee -a "$LOG"
    break
  fi
  sleep 2
done
if ! ss -tlnp "sport = :7456" 2>/dev/null | grep -q LISTEN; then
  echo "[od] ERROR: Daemon failed to start" | tee -a "$LOG"
  exit 1
fi

# Step 2: web + landing (可并行)
echo "[od] Starting web (port 3000)..." | tee -a "$LOG"
nohup pnpm --filter @open-design/web dev >> "$LOG" 2>&1 &
BG_PIDS+=($!)

echo "[od] Starting landing (port 17574)..." | tee -a "$LOG"
nohup pnpm --filter @open-design/landing-page dev >> "$LOG" 2>&1 &
BG_PIDS+=($!)

# 等待两者就绪
for i in {1..30}; do
  w=$(ss -tlnp "sport = :3000" 2>/dev/null | grep -c LISTEN || true)
  l=$(ss -tlnp "sport = :17574" 2>/dev/null | grep -c LISTEN || true)
  if [ "$w" -ge 1 ] && [ "$l" -ge 1 ]; then
    echo "[od] All services ready — http://localhost:3000 / http://localhost:17574" | tee -a "$LOG"
    exit 0
  fi
  sleep 1
done

echo "[od] WARNING: Some services may not have started. Check $LOG" | tee -a "$LOG"
exit 1
