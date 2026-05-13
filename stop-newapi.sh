#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="new-api"

echo "[newapi] Stopping New API..."

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  docker stop "$CONTAINER_NAME" >/dev/null
  echo "[newapi] Stopped."
else
  echo "[newapi] Container not found."
fi
