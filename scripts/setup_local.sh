#!/usr/bin/env bash
# 本地一键起（macOS + brew Postgres，无需 Docker）：
#   ./scripts/setup_local.sh
# 会：① 确保 brew Postgres 运行 ② 建库 ③ 迁移+种子 ④ 起 uvicorn（默认 Ollama 真实模型）
set -euo pipefail

# brew Postgres 客户端路径
export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"

PG_USER="${PG_USER:-$USER}"
DB_NAME="${DB_NAME:-medical_agent}"
DB_URL="postgresql+psycopg2://${PG_USER}@localhost:5432/${DB_NAME}"

echo "==> 1/4 确保 Postgres 运行"
if ! pg_isready -h localhost -p 5432 >/dev/null 2>&1; then
  echo "    启动 brew Postgres..."
  brew services start postgresql@18 >/dev/null 2>&1 || \
    pg_ctl -D "$(brew --prefix)/var/postgresql@18" -l /tmp/pg.log start
  sleep 3
fi

echo "==> 2/4 建库（若不存在）"
if ! psql -U "$PG_USER" -h localhost -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  psql -U "$PG_USER" -h localhost -d postgres -c "CREATE DATABASE ${DB_NAME};"
fi

echo "==> 3/4 迁移 + 种子"
export DATABASE_URL="$DB_URL"
export LLM_MODE="${LLM_MODE:-ollama}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b}"
.venv/bin/python scripts/migrate.py
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from src.seed import seed_all; seed_all()"

echo "==> 4/4 启动网关 (http://localhost:8000)"
echo "    患者端: client/chat.html   医护端: client/review.html"
.venv/bin/uvicorn src.gateway:app --reload --port 8000
