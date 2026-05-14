#!/usr/bin/env bash
set -euo pipefail
echo "[od] Stopping all Open Design services..."

for port in 7456 3000 17574; do
  PID=$(ss -tlnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)
  if [ -z "$PID" ]; then
    PID=$(lsof -ti :$port 2>/dev/null || true)
  fi
  if [ -n "$PID" ]; then
    CMD=$(ps -p "$PID" -o cmd= 2>/dev/null || true)
    if [[ "$CMD" == *"open-design"* || "$CMD" == *"pnpm"* || "$CMD" == *"node"* ]]; then
      echo "[od] Killing $port (pid $PID)..."
      kill "$PID" 2>/dev/null || true
      sleep 0.5
      kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    else
      echo "[od] ERROR: PID $PID 不匹配预期进程 (cmd: $CMD)"
      exit 1
    fi
  else
    echo "[od] Port $port — nothing to stop"
  fi
done

echo "[od] All stopped."
