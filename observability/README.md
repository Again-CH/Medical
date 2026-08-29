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
│   ├── alertmanager.yml            # 告警路由 → 本地 alert-sink（可跑可看可测）
│   ├── alertmanager.production.yml # 生产版：url_file 引用企业 IM 密钥（URL 不入库）
│   └── secrets/                    # 生产 webhook 密钥挂载点（.gitignore 排除）
└── alert-sink-data/                # 本地接收端落盘的告警 JSONL（运行时产物）
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
#    Alert Sink  http://localhost:9101/health
```

## 告警闭环：如何证明告警真的发出去了

配置里写 receiver 不等于告警能送达。原先 `alertmanager.yml` 用的是 `null` receiver
占位 —— 栈能起来，但告警哪儿也去不了。现在改成真实 webhook，指向随栈拉起的本地接收端
`alert-sink`（`scripts/alert_sink.py`，纯标准库，无额外依赖）。

**手工触发一条测试告警，验证整条链路：**

```bash
# 1) 发一条告警给 Alertmanager
curl -XPOST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[
  {"labels":{"alertname":"SmokeTest","severity":"critical","instance":"local"},
   "annotations":{"summary":"告警闭环冒烟测试"}}]'

# 2) 看接收端是否收到（出现可读摘要即闭环成功）
docker compose logs -f alert-sink
#   [CRITICAL] firing SmokeTest @ local :: 告警闭环冒烟测试

# 3) 看落盘证据
cat alert-sink-data/alerts.jsonl
```

接收端行为：解析 Alertmanager v4 载荷 → 按严重度排序打印摘要 → 追加写入
`alert-sink-data/alerts.jsonl`（每行一条 JSON，便于 `jq` / ELK 消费）。
`send_resolved: true`，故障恢复也会推送（status=resolved），闭环完整。

> 也可以不依赖 Docker 单独验证接收端：
> `python scripts/alert_sink.py --port 9101 --log /tmp/alerts.jsonl`

## 分级路由与抑制

| 级别 | 场景 | group_wait | repeat_interval |
| ---- | ---- | ---------- | --------------- |
| critical | 安全闸突增、错误率破 SLO | 10s | 30m |
| warning  | SLO 边缘、熔断开启、降级突增 | 1m | 4h |
| info     | kill switch 激活（已知运维动作） | 5m | 24h |

**抑制规则**：同一 `alertname` 在 critical 触发时抑制它的 warning/info ——
一个故障刷出一屏告警会造成告警疲劳，比漏报更伤响应效率。

## 生产：换成企业 IM / 电话值班

本地用 sink 是为了**零凭证即可验证链路**；生产请切到 `alertmanager.production.yml`，
它与本地版的路由、抑制、时序完全一致，只改投递目标。

关键点是用 `url_file` 而不是 `url`：企业微信/钉钉/飞书机器人 webhook 形如
`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=XXXX`，**key 就是凭证**，
写进配置文件等于把密钥提交进版本库。`url_file` 让 Alertmanager 从挂载的 Secret 读取。

```bash
# K8s：Secret 挂到 /etc/alertmanager/secrets/，配置文件无需任何改动
kubectl create secret generic alert-webhook \
  --from-literal=critical_url="$WECOM_CRITICAL_URL" \
  --from-literal=warning_url="$WECOM_WARNING_URL"
```

`observability/alertmanager/secrets/` 已被 `.gitignore` 排除，本地填真实 URL 调试也不会误提交。

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
