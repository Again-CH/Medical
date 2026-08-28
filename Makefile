# 常用命令（本地 brew Postgres 场景）
# 注意：Makefile 要求 Tab 缩进，请勿替换为空格

PG_USER ?= $(USER)
DB_URL ?= postgresql+psycopg2://$(PG_USER)@localhost:5432/medical_agent

.PHONY: db-up migrate seed run test lint format eval ci-local

db-up:
	./scripts/setup_local.sh

migrate:
	DATABASE_URL=$(DB_URL) .venv/bin/python scripts/migrate.py

seed:
	DATABASE_URL=$(DB_URL) .venv/bin/python -c "import sys; sys.path.insert(0,'.'); from src.seed import seed_all; seed_all()"

run:
	DATABASE_URL=$(DB_URL) LLM_MODE=ollama OLLAMA_MODEL=qwen2.5:1.5b \
		.venv/bin/uvicorn src.gateway:app --reload --port 8000

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

format:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

eval:
	.venv/bin/python scripts/eval_offline.py

# 本地模拟 CI：lint + test + eval 一遍
ci-local: lint test eval
