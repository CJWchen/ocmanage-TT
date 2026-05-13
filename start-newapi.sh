#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/yun/桌面/workspace/new-api"
DATA_DIR="$APP_DIR/data"
CONTAINER_NAME="new-api"
IMAGE="calciumion/new-api:latest"
PORT=3000

echo "[newapi] Starting New API..."
mkdir -p "$DATA_DIR"

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" == "true" ]]; then
    echo "[newapi] Container is already running."
    exit 0
  fi
  docker start "$CONTAINER_NAME"
else
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart always \
    -p "$PORT:3000" \
    -v "$DATA_DIR:/data" \
    -e TZ=Asia/Shanghai \
    "$IMAGE" >/dev/null
fi

for i in {1..30}; do
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || echo false)" == "true" ]]; then
    echo "[newapi] Container ready at http://localhost:$PORT"
    exit 0
  fi
  sleep 1
done

echo "[newapi] ERROR: Container failed to start."
docker logs --tail 40 "$CONTAINER_NAME" 2>/dev/null || true
exit 1
