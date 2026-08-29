# 常用命令（本地 brew Postgres 场景）
# 注意：Makefile 要求 Tab 缩进，请勿替换为空格

PG_USER ?= $(USER)
DB_URL ?= postgresql+psycopg2://$(PG_USER)@localhost:5432/medical_agent

.PHONY: db-up migrate seed run test lint format eval check-migrations ci-local \
	helm-lint helm-template gen-secrets install-chart

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

# schema 漂移检查：ORM 模型必须与 Alembic 迁移一致（默认临时 SQLite，无需外部库）
check-migrations:
	.venv/bin/python scripts/check_migrations.py

# 本地模拟 CI：lint + 漂移检查 + test + eval 一遍
ci-local: lint check-migrations test eval

# ---- IaC（Helm）----
# 校验 chart（需本地 helm；未安装时提示跳过）
helm-lint:
	@if command -v helm >/dev/null 2>&1; then \
		helm lint ./charts/medical-agent; \
	else \
		echo "helm 未安装，跳过 helm-lint（可用 'brew install helm' 后重试）"; \
	fi

# 渲染模板为 YAML（dry-run，不连集群），用于评审/CI 静态校验
helm-template:
	@if command -v helm >/dev/null 2>&1; then \
		helm template medical-agent ./charts/medical-agent --set secrets.create=true \
			--set secrets.data.jwtSecret=demo --set secrets.data.adminApiKey=demo \
			--set secrets.data.databaseUrl=demo >/dev/null && echo "helm template OK"; \
	else \
		echo "helm 未安装，跳过 helm-template"; \
	fi

# 生成随机强密钥 Secret 清单（不落盘，直接 kubectl apply）
gen-secrets:
	bash scripts/gen-secrets.sh

# 部署到集群（demo：随 chart 起 Postgres，须显式给口令）
install-chart:
	helm install medical-agent ./charts/medical-agent -n medical-agent --create-namespace \
		--set postgresql.enabled=true \
		--set postgresql.auth.password="$$(openssl rand -base64 24)"
