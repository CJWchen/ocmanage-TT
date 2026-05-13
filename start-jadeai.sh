#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/yun/desktop/workspace/JadeAI"
DATA_DIR="$APP_DIR/jadeai-data"
CONTAINER_NAME="jadeai"
IMAGE="twwch/jadeai:latest"
PORT=3003

echo "[jadeai] Starting JadeAI..."
mkdir -p "$DATA_DIR"

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" == "true" ]]; then
    echo "[jadeai] Container is already running."
    exit 0
  fi
  docker start "$CONTAINER_NAME"
else
  docker run -d \
    --name "$CONTAINER_NAME" \
    --platform linux/amd64 \
    -p "$PORT:3000" \
    -e AUTH_SECRET=wmd/jfIiNq9bEiDnSjqShUFzNLomIHFJWxOSQd2jnhc= \
    -e AUTH_ENABLED=false \
    -e DB_TYPE=sqlite \
    -v "$DATA_DIR:/app/data" \
    "$IMAGE" >/dev/null
fi

for i in {1..30}; do
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || echo false)" == "true" ]]; then
    echo "[jadeai] Container ready at http://localhost:$PORT"
    exit 0
  fi
  sleep 1
done

echo "[jadeai] ERROR: Container failed to start."
docker logs --tail 40 "$CONTAINER_NAME" 2>/dev/null || true
exit 1
