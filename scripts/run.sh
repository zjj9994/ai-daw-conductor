#!/usr/bin/env bash
# 启动本地服务
set -e
cd "$(dirname "$0")/.."
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
HOST="${SERVER_HOST:-127.0.0.1}"
PORT="${SERVER_PORT:-8787}"
echo "▶ 启动 AI-DAW-Conductor  http://${HOST}:${PORT}"
exec python3 -m uvicorn backend.server:app --host "$HOST" --port "$PORT"
