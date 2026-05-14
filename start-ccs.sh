#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
CCS_DIR="$WORKSPACE_DIR/cc-switch"
LOG_DIR="${XDG_RUNTIME_DIR:-${HOME}/.local/state}/manager-tt"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/ccs-dev.log"
DEV_PORT=3001
RUST_TOOLCHAIN="1.95.0-x86_64-unknown-linux-gnu"
RUST_BIN="/home/yun/.rustup/toolchains/${RUST_TOOLCHAIN}/bin"
CARGO_BIN="${RUST_BIN}/cargo"
RUSTC_BIN="${RUST_BIN}/rustc"

echo "[ccs] Starting CC Switch..."
echo "[ccs] Log file: $LOG"
mkdir -p "$(dirname "$LOG")"
touch "$LOG"

if [[ ! -x "${CARGO_BIN}" || ! -x "${RUSTC_BIN}" ]]; then
  echo "[ccs] ERROR: cargo/rustc not found in ${RUST_BIN}" | tee -a "$LOG"
  exit 1
fi

if ss -ltn "sport = :$DEV_PORT" 2>/dev/null | grep -q LISTEN; then
  echo "[ccs] Port $DEV_PORT is already listening, assuming service is already running."
  exit 0
fi

if [[ ! -d "$CCS_DIR/node_modules" ]]; then
  echo "[ccs] Installing dependencies..." | tee -a "$LOG"
  (cd "$CCS_DIR" && pnpm install) >> "$LOG" 2>&1
fi

echo "[ccs] Starting Tauri dev app on port $DEV_PORT..." | tee -a "$LOG"
cd "$CCS_DIR"
nohup bash -lc '
  export PATH="'"${RUST_BIN}"':$PATH"
  export RUSTUP_TOOLCHAIN="'"${RUST_TOOLCHAIN}"'"
  export CARGO="'"${CARGO_BIN}"'"
  export RUSTC="'"${RUSTC_BIN}"'"
  pnpm tauri dev --runner "'"${CARGO_BIN}"'" -c "{\"build\":{\"devUrl\":\"http://localhost:3001\",\"beforeDevCommand\":\"pnpm exec vite --port 3001 --strictPort\"}}"
' >> "$LOG" 2>&1 &

for i in {1..60}; do
  if ss -ltn "sport = :$DEV_PORT" 2>/dev/null | grep -q LISTEN; then
    echo "[ccs] Dev server ready at http://localhost:$DEV_PORT" | tee -a "$LOG"
    exit 0
  fi
  sleep 2
done

echo "[ccs] WARNING: Port $DEV_PORT is not ready yet. Check $LOG" | tee -a "$LOG"
exit 1
