#!/usr/bin/env bash
# 生成 medical-agent 所需的 Kubernetes Secret 清单（随机强密钥）。
#
# 用法：
#   bash scripts/gen-secrets.sh [namespace]
#   DB_HOST=my-rds.region.rds.amazonaws.com bash scripts/gen-secrets.sh prod
#
# 输出可直接 `kubectl apply -f -`（或重定向到文件后，用 SealedSecrets 加密再提交）。
# 设计原则：密钥一律随机生成、不落盘、不进 Git；缺失即拒绝启动（fail-closed）。
set -euo pipefail

NS="${1:-medical-agent}"
DB_USER="${DB_USER:-med_user}"
DB_HOST="${DB_HOST:-postgresql}"
DB_NAME="${DB_NAME:-med_agent}"
DB_PORT="${DB_PORT:-5432}"
CORS_ORIGINS="${CORS_ORIGINS:-https://your-domain.com}"

DB_PASS="$(openssl rand -base64 24)"
JWT_SECRET="$(openssl rand -base64 48)"
ADMIN_API_KEY="$(openssl rand -base64 32)"

cat <<EOF
# 由 scripts/gen-secrets.sh 生成（$(date -u +%Y-%m-%dT%H:%M:%SZ)）。
# 生产请用 SealedSecrets / External Secrets Operator 管理，避免明文留存。
apiVersion: v1
kind: Secret
metadata:
  name: medical-agent-secrets
  namespace: ${NS}
type: Opaque
stringData:
  database-url: "postgresql+psycopg2://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
  jwt-secret: "${JWT_SECRET}"
  admin-api-key: "${ADMIN_API_KEY}"
  cors-origins: "${CORS_ORIGINS}"
EOF
