#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="jadeai"

echo "[jadeai] Stopping JadeAI..."

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  docker stop "$CONTAINER_NAME" >/dev/null
  echo "[jadeai] Stopped."
else
  echo "[jadeai] Container not found."
fi
