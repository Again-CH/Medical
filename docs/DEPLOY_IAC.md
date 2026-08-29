# 部署即代码（IaC）与密钥管理

本目录提供 Kubernetes 部署包（`charts/medical-agent/`），把已有的
`docker-compose.yml` / `Dockerfile` 能力平移到 K8s，并补齐「密钥管理」这一
生产化必答题。

## 1. 这套 Chart 解决了什么

| 维度 | docker-compose | Helm Chart |
| --- | --- | --- |
| 密钥 | `${VAR:?}` 缺失即失败 | `envFrom.secretRef` 缺失 → Pod 起不来（等价 fail-closed） |
| 配置 | 环境变量 | ConfigMap（非密）/ Secret（敏感）分离 |
| 扩缩容 | 手动 | HPA（CPU 目标 80%） |
| 入口 | 宿主机端口 | Ingress（可配 TLS） |
| 安全基线 | `no-new-privileges` + `cap_drop: ALL` | 非 root + `readOnlyRootFilesystem` + `drop: ALL` |
| 数据库 | 同 compose 网络 | 可选随 chart 部署 Postgres，或接托管 RDS |

## 2. 目录结构

```
charts/medical-agent/
├── Chart.yaml
├── values.yaml                 # 非密默认值；secrets 默认不提交明文
├── secrets.example.yaml        # Secret 结构示例（占位，勿提交真值）
├── templates/
│   ├── _helpers.tpl
│   ├── configmap.yaml          # APP_ENV / LLM_EGRESS_POLICY / CSP_STRICT ...
│   ├── secret.yaml             # 仅当 secrets.create=true 时生成（demo 用）
│   ├── deployment.yaml         # 核心：envFrom 分离 + 安全上下文
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── hpa.yaml
│   ├── postgresql.yaml         # 可选：随 chart 的 Postgres（demo）
│   └── NOTES.txt
└── .helmignore
scripts/gen-secrets.sh          # 随机生成 Secret 清单（不落盘）
```

## 3. 密钥管理（核心）

应用启动期强校验要求 `DATABASE_URL` / `JWT_SECRET` / `ADMIN_API_KEY` /
`CORS_ORIGINS` 齐备，缺失即拒绝启动（对应 `APP_ENV=production` 下的安全门）。
因此 **K8s Secret 是强制依赖**，Chart 不提供任何弱口令默认值。

三种落地方式（按生产推荐度排序）：

1. **External Secrets Operator（推荐生产）**
   在云厂商密钥管理（AWS Secrets Manager / 阿里云 KMS / Vault）存放真值，
   ESO 同步为集群内 `Secret` `medical-agent-secrets`，Chart 用
   `secrets.existingSecret=medical-agent-secrets` 引用即可。密钥永不进 Git。

2. **SealedSecrets（推荐自建集群）**
   `kubectl create secret generic medical-agent-secrets ...` 后用
   `kubeseal` 加密成 `SealedSecret` 提交仓库，Controller 解密回 `Secret`。

3. **随 chart 生成（仅 demo / 非生产）**
   `helm install ... --set secrets.create=true --set secrets.data.jwtSecret=...`
   或 `bash scripts/gen-secrets.sh | kubectl apply -f -`。
   **提交前务必清空 `secrets.data`，否则密钥进 Git 历史。**

Secret 必须包含键（与 `secret.yaml` / `secrets.example.yaml` 一致）：
`database-url`、`jwt-secret`、`admin-api-key`、`cors-origins`。

## 4. 使用

```bash
# 校验（需本地 helm）
make helm-lint
make helm-template

# 生成密钥并创建
bash scripts/gen-secrets.sh medical-agent | kubectl apply -f -

# 部署（默认不随 chart 起 Postgres，DATABASE_URL 指向托管库）
helm install medical-agent ./charts/medical-agent -n medical-agent --create-namespace

# 或：随 chart 起 Postgres（demo 用，须显式给口令）
helm install medical-agent ./charts/medical-agent -n medical-agent --create-namespace \
  --set postgresql.enabled=true \
  --set postgresql.auth.password="$(openssl rand -base64 24)"
```

## 5. 安全基线说明

- **非 root**：`runAsNonRoot: true, runAsUser: 10001`（与 Dockerfile 的
  `appuser` uid 对齐）。
- **只读根文件系统**：`readOnlyRootFilesystem: true`；仅 `/app/data`
  （患者私有库 sqlite 与运行时产物）挂 `emptyDir` 可写。
  > 注意：患者私有库以 sqlite 文件落在 `/app/data` 是 demo 形态；生产应改为
  > 对象存储 / 托管库，使 Pod 可无状态水平扩展。
- **最小权限**：`allowPrivilegeEscalation: false` + `capabilities.drop: [ALL]`。
- **密钥轮转**：Deployment 注解 `checksum/secrets` 让 Secret 变更触发滚动重启，
  新密钥即时生效（配合短时效 JWT 与 `token_version` 吊销）。

## 6. 平移关系（compose → K8s）

| compose 变量 | K8s 来源 |
| --- | --- |
| `${POSTGRES_PASSWORD:?}` | `Secret.data.database-url`（完整连接串） |
| `${JWT_SECRET:?}` | `Secret.data.jwt-secret` |
| `${ADMIN_API_KEY:?}` | `Secret.data.admin-api-key` |
| `${CORS_ORIGINS:?}` | `Secret.data.cors-origins` |
| `LLM_EGRESS_POLICY=strict` 等 | `ConfigMap` |
| `cap_drop: ALL` | `securityContext.capabilities.drop` |
