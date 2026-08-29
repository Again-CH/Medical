# 可观测性栈（Observability）

把「能看指标」升级成「有人响应」：Prometheus 抓取 + Grafana 面板 + Alertmanager 告警，
围绕本项目已有的 `/metrics` 端点（`src/metrics.py`）构建。

## 目录结构

```
observability/
├── docker-compose.yml          # 一键拉起 prometheus + grafana + alertmanager
├── README.md                   # 本文件
├── SLO.md                      # SLO 目标、错误预算与告警分级口径
├── prometheus/
│   ├── prometheus.yml          # 抓取配置（Bearer 携带 ADMIN_API_KEY）
│   └── alert_rules.yml         # 7+ 条 SLO/异常告警规则
├── alertmanager/
│   └── alertmanager.yml        # 告警路由（默认 null receiver，生产接 IM）
└── grafana/
    ├── provisioning/           # 数据源 + 面板自动导入
    │   ├── datasources/datasource.yml
    │   └── dashboards/dashboards.yml
    └── dashboards/
        └── medical-agent.json  # 医疗 Agent 可观测面板
```

## 快速开始

```bash
cd observability

# 1) 写入抓取密钥（必须与启动应用时的 ADMIN_API_KEY 一致）
echo "你的ADMIN_API_KEY" > prometheus/admin_key

# 2) 启动栈
docker compose up -d

# 3) 访问
#    Grafana     http://localhost:3000   (admin / admin)  → 已自动导入「医疗 Agent 可观测面板」
#    Prometheus  http://localhost:9090   → Status > Targets 确认 medical-agent 为 UP
#    Alertmanager http://localhost:9093
```

## 指标鉴权（重要）

应用 `/metrics` 默认需管理员密钥，避免把路由清单/流量特征匿名暴露。
Prometheus 无法发送任意自定义头，但支持 `authorization.credentials_file`
（见 `prometheus.yml`）——把同一把 `ADMIN_API_KEY` 存成 `admin_key` 文件即可
以 Bearer 形式安全抓取。**不要**为图省事把 `METRICS_PUBLIC=1` 设为公开指标
（除非是受信任的内网 / sidecar 场景）。应用侧 `_require_admin_key` 同时接受
`X-Admin-Key` 与 `Authorization: Bearer` 两种传法。

## 应用侧的运维端点（均需 X-Admin-Key）

| 端点                         | 用途                                       |
| ---------------------------- | ------------------------------------------ |
| `GET  /metrics`              | Prometheus 抓取点                          |
| `GET  /api/admin/resilience` | 熔断器状态 + kill switch 清单              |
| `POST /api/admin/killswitch` | 运行时停用/启用某工具或意图（摘流量）      |
| `POST /api/admin/breaker/reset` | 手动复位熔断器                          |
| `GET  /api/admin/cost`       | LLM 成本归因：按患者/Agent/模型三维聚合    |

## 关于 LLM 成本归因

`GET /api/admin/cost` 返回结构（`src/cost.cost_breakdown`）：

```json
{
  "model": "gpt-4o-mini",
  "totals": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0 },
  "by_patient": [ { "key": "alice", "total_tokens": 0, "cost_usd": 0.0 } ],
  "by_agent":    [ { "key": "triage", ... } ],
  "by_model":    [ { "key": "gpt-4o-mini", ... } ]
}
```

> 注意：患者维度是**进程内分账**，重启或多 worker 会清零（已知边界）；
> agent/model 维度同步进入 Prometheus TSDB 长期留存。生产若需患者级持久化，
> 可把 ledger 落库或接 LangSmith 的 token 计费。
