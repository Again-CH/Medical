#!/usr/bin/env bash
# E2E 测试用的后端启动脚本（被 playwright.config.ts 的 webServer 调用）。
# 使用一次性 sqlite 库 + fake LLM 模式，无需 Postgres / 外部 API key，自包含可复现。
set -euo pipefail

cd "$(dirname "$0")/.."   # 切到仓库根目录

export DATABASE_URL="${DATABASE_URL:-sqlite:///./e2e.db}"
export LLM_MODE="${LLM_MODE:-fake}"
export JWT_SECRET="${JWT_SECRET:-e2e-dev-secret-not-for-prod-use-only-0123456789}"
export PORT="${PORT:-8137}"

PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then PY="python3"; fi

echo "[e2e-server] DATABASE_URL=$DATABASE_URL LLM_MODE=$LLM_MODE PORT=$PORT"
exec "$PY" -m uvicorn src.gateway:app --host 127.0.0.1 --port "$PORT"
