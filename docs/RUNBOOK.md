# 运维处置手册（Runbook）

> **这是给「凌晨三点被告警叫醒的人」看的。** 目标和体检报告不同：体检报告回答「这个项目做得怎么样」，本手册回答「出事了第一步做什么」。
>
> 配套文档：[`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md)（架构与全貌）、[`observability/SLO.md`](../observability/SLO.md)（SLO 与错误预算口径）、[`../企业级体检报告.html`](../企业级体检报告.html)（横向评分）。
>
> **打印/收藏建议**：第 1 节（黄金五分钟）、第 4 节（通用操作速查）、第 6 节（命令速查表）。

---

## 目录

1. [怎么用这份手册](#1-怎么用这份手册)
2. [严重度定义与响应时限](#2-严重度定义与响应时限)
3. [告警逐条处置](#3-告警逐条处置)
4. [通用运维操作](#4-通用运维操作)
5. [升级矩阵](#5-升级矩阵)
6. [命令速查表](#6-命令速查表)

---

## 1. 怎么用这份手册

### 黄金五分钟（先止损，再查因）

```
① 确认影响面   → 患者还能不能完成对话/挂号？是单院区还是全站？
② 定位分层     → 应用 / 依赖（LLM、DB、HIS） / 基础设施（K8s、网络）
③ 先止损       → 摘流量（kill switch）或回滚，别在故障中调试
④ 再查因       → 日志 → 指标 → 链路
⑤ 记录与复盘   → 时间线写进事故单（见第 5 节）
```

> **顺序不能反。** 线上排查的诱惑是「先看看日志找根因」，但医疗系统里**患者可用性优先**——先恢复服务，根因可以事后查（服务恢复了才有从容查的时间）。

### 第一步：确认影响面（30 秒内）

```bash
# 1) 服务是否活着
curl -fsS https://<host>/health && echo "OK"

# 2) 韧性状态：有无熔断/停用（多数故障在这就能定性）
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/api/admin/resilience | jq

# 3) 是哪个环境/版本
kubectl -n medical-<env> get deploy medical-medical-agent \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

`resilience` 返回中 `breakers[].state` 若为 `open` → 依赖被隔离（见 3.6）；`killswitch.active > 0` → 有人摘过流量（见 3.7）。

### 分层定位速判

| 现象 | 大概率在哪层 | 去哪节 |
|---|---|---|
| `/health` 不通、Pod 未 Ready | 基础设施/应用 | 3.1 |
| 5xx 突增 | 应用或下游 | 3.1 |
| 响应慢但没报错 | LLM 或 DB 慢 | 3.2 / 3.3 |
| 大量「暂不可用」「稍后再试」 | 降级生效（熔断/kill switch） | 3.6 / 3.7 |
| 患者说「约不上号」 | 号源耗尽或排班未生成 | 3.4 |
| 审批单堆积 | 医护侧人力不足 | 3.5 |
| 安全闸频繁命中 | 攻击试探或提示词注入 | 3.8 |

---

## 2. 严重度定义与响应时限

| 级别 | 含义 | 响应时限 | 通知方式 | 对应告警 |
|---|---|---|---|---|
| **critical** | 患者可用性受损，或存在安全风险 | **15 分钟**内响应 | 电话/短信叫醒 | `MedicalAgentHighErrorRate`、`MedicalAgentSafetyGateSpike` |
| **warning** | SLO 边缘或运维积压，暂不影响主流程 | 1 小时（工作时间）/ 4 小时（夜间） | IM 群 | 首字节延迟、LLM 延迟、审批积压、熔断、超时、降级 |
| **info** | 已知运维动作，仅备案 | 下一个工作日 | 邮件/工单 | `MedicalAgentKillSwitchActive` |

> **注意**：`info` 不代表可以忽略。`MedicalAgentKillSwitchActive` 持续 5 分钟以上说明**有人摘了流量但没恢复**——本该是临时措施的操作变成了长期状态，功能被静默阉割。

---

## 3. 告警逐条处置

### 3.1 `MedicalAgentHighErrorRate`（critical）

- **表达式**：`5xx 占比 > 0.5%` 持续 5 分钟
- **含义**：对话接口可用性跌破 SLO（99.5%）

**判定**

```bash
# 哪个路由在报 5xx（按路由模板，不会因 path 参数产生高基数）
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/metrics \
  | grep 'medical_agent_http_requests' | grep 'status="5' 

# 应用日志
kubectl -n medical-<env> logs -l app.kubernetes.io/name=medical-agent --tail=200 | grep -i error
```

**处置**

1. 若是**刚发版**后出现 → 立即回滚（见 [4.3 回滚](#43-回滚)）。发版是 5xx 突增的第一嫌疑。
2. 若是**依赖故障**（DB/HIS/LLM）→ 摘流量止损（见 [4.1 kill switch](#41-kill-switch-摘流量)），让系统走安全降级。
3. 若是**容量不足**（请求量涨）→ 扩容（见 [4.4 扩容](#44-扩容)）。

**恢复验证**：5xx 占比回落到 0.5% 以下并稳定 10 分钟。

**升级条件**：15 分钟内未恢复，或影响到挂号主链路 → 升级至应用负责人。

---

### 3.2 `MedicalAgentFirstTokenSLO`（warning）

- **表达式**：首字节延迟 p95 > 2s 持续 10 分钟
- **含义**：患者发出消息到看到第一个字的体感变卡。**这是患者唯一能直接感知的指标**，权重应高于吞吐。

**判定**：拆开看是哪一段慢——

```bash
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/metrics | grep -E \
  'medical_agent_chat_first_token_seconds|medical_agent_chat_duration_seconds|medical_agent_llm_duration_seconds'
```

- 首 token 慢、总耗时也慢 → LLM 慢（看 3.3）
- 首 token 快、总耗时长 → 工具调用慢（DB/HIS）
- 都慢 → 应用本身或资源不足（看 CPU/内存是否打满）

**处置**：按上面对应段落处理；若属资源不足 → 扩容。

**注意**：若系统正走**安全降级**，首字节会很快但内容质量下降——此时该看 3.9 而不是本条。

---

### 3.3 `MedicalAgentLLMLatencySLO`（warning）

- **表达式**：LLM 调用延迟 p95 > 2s 持续 10 分钟

**判定**

- 云端模型（Deepseek 等）：可能是供应商限速或网络抖动。查供应商状态页。
- 本地 Ollama：查 GPU/CPU 是否过载、模型是否被换成了更大的、并发是否超了实例能力。

```bash
# 本地模型场景
kubectl -n medical-<env> top pod -l app.kubernetes.io/name=medical-agent
curl -s http://<ollama-host>:11434/api/ps    # 当前加载了哪些模型、显存占用
```

**处置**

1. 确认 LLM 是否整体不可用 → 若是，系统会自动走安全降级；确认降级话术对患者可见且友好。
2. 若只是慢 → 可临时停用 LLM 密集型意图（`agent:triage`）保住挂号主链路。
3. 若持续超 30 分钟 → 考虑切备用模型或降级到 KB 直出。

**升级条件**：LLM 完全不可用超 15 分钟 → 升级（患者侧已大面积走兜底话术）。

---

### 3.4 `MedicalAgentApprovalBacklog`（warning）

- **表达式**：待审批 > 20 单持续 10 分钟
- **含义**：**这是唯一一条「系统没问题、人没跟上」的告警**——敏感操作（医保结算/转诊/120）在等医护点确认，患者正在干等。

**判定**

```bash
curl -fsS -H "Authorization: Bearer $DOCTOR_TOKEN" https://<host>/api/review/pending | jq 'length'
```

**处置**

1. **先判断是不是人力问题**：查看积压时长分布——若集中在某个班次，是排班问题，通知科室加人。
2. 若是**系统问题**（审批单卡在 PENDING 无法流转）→ 查 `pending_calls` 表与 checkpointer 是否正常。
3. **不要**为了清积压而批量批准——每一单都含完整 args 与 requester，审批人必须逐单确认。

> 设计约束：审批门是安全边界，**不可配置关闭**。宁可让患者等，也不能让敏感操作绕过人工确认。

---

### 3.5 `MedicalAgentChatTimeoutSpike`（warning）

- **表达式**：对话超时 > 5 次 / 10 分钟
- **含义**：LLM 或下游卡死，患者侧已返回友好错误（不会一直转圈）

**判定**：查链路追踪——用告警里的 trace-id 在 Jaeger/Grafana Tempo 里打开，看卡在哪个 span。

**处置**

1. 若集中在 LLM span → 同 3.3。
2. 若集中在某个工具 span → 该下游有问题，停用对应工具（见 4.1）。
3. **注意超时不等于失败**：患者已收到明确提示，不会无限等待。所以这条通常是**症状**而非根因，要顺着追下去。

---

### 3.6 `MedicalAgentBreakerOpen`（warning）

- **表达式**：10 分钟内有熔断器开启
- **含义**：某依赖连续失败，已被**快速失败**隔离。这是**保护动作**，不是故障本身——系统正在按设计止损。

**关键认知**：熔断开启后，对该依赖的调用会立即失败而不再等待超时，这防止了「重试叠加超时 → 协程耗尽 → 雪崩」。**所以不要一看到熔断就去复位。**

**判定**

```bash
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/api/admin/resilience | jq '.breakers'
# 关注 name / state / failure_count / last_failure
```

**处置**

1. **先确认依赖真的恢复了**，再复位。没恢复就复位 = 让流量重新冲击故障依赖，可能把刚稳住的系统再次打挂。
2. 确认恢复后：

```bash
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"<breaker-name>"}' https://<host>/api/admin/breaker/reset
# 省略 name 则复位全部（谨慎）
```

3. 熔断**反复开启**说明依赖处于不稳定边缘 → 不要反复复位，改走 kill switch 长期摘流量 + 通知依赖方。

---

### 3.7 `MedicalAgentKillSwitchActive`（info）

- **表达式**：有工具/意图处于停用状态且持续 5 分钟
- **含义**：有人（或某次故障处置）摘了流量，**但没有恢复**。功能被静默阉割中。

**判定与处置**

```bash
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/api/admin/resilience | jq '.killswitch'
# 依赖恢复后重新启用：
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"target":"<tool-name>","disabled":false}' https://<host>/api/admin/killswitch
```

> **纪律要求**：每次启用 kill switch 都应在工单里记录**谁、何时、为什么、预计何时恢复**。本告警存在的意义就是提醒「别忘了把它开回去」。

---

### 3.8 `MedicalAgentSafetyGateSpike`（critical）

- **表达式**：Tier-0 安全闸 10 分钟命中 > 30 次
- **含义**：急症/定位违规/知情同意硬闸频繁命中——**可能是攻击试探或提示词注入**，不是普通故障。

**判定（先分清敌我）**

```bash
# 看是哪类红线被触发、集中在哪些患者/会话
kubectl -n medical-<env> logs -l app.kubernetes.io/name=medical-agent --tail=500 \
  | grep -i 'safety_gate\|redline'

# 审计日志里找异常模式（同一 IP/会话高频触发）
```

- **集中在少数会话/IP** → 攻击试探，封禁来源。
- **分散在大量正常会话** → 可能是**提示词/规则误伤**（比如新上线的科室名触发了定位违规），属**误报**，应调整规则而非封禁患者。

**处置**

1. 攻击场景 → 封禁来源 IP/账号；保留日志作为证据；必要时上报安全。
2. 误伤场景 → 回滚最近的规则/知识库变更；**不要**为了让告警消失而放宽安全闸。
3. 急症命中（患者真在求助）→ 系统已引导 120 或急诊，**人工复核是否有漏引导**，这是唯一可能造成伤害的场景。

**升级条件**：**所有 critical 里优先级最高的一条**，涉及患者安全，应立即通知安全负责人与应用负责人。

---

### 3.9 `MedicalAgentLLMFallbackSpike`（warning）

- **表达式**：10 分钟内走安全兜底 > 10 次
- **含义**：大量请求因 LLM 失败走了兜底话术。**患者仍能用，但体验退化**——不是中断，是"变笨了"。

**判定**：区分降级原因——熔断 / 超时 / LLM 异常。

**处置**

1. 若因熔断 → 见 3.6。
2. 若因 LLM 不稳定 → 见 3.3。
3. **特别关注**：降级话术是否**足够安全**。本项目降级回退是「明确说明无法回答 + 引导就医」，**绝不编造**——若发现兜底输出了具体医学建议，属**严重缺陷**，立即按 critical 处理并回滚。

---

## 4. 通用运维操作

### 4.1 kill switch（摘流量）

下游（HIS/短信网关）宕机时，**不发版**即可摘掉故障依赖，系统自动走安全降级。

```bash
# 停用某个工具
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"target":"query_availability","disabled":true}' https://<host>/api/admin/killswitch

# 停用整个意图（前缀 agent:）
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"target":"agent:triage","disabled":true}' https://<host>/api/admin/killswitch

# 恢复
... -d '{"target":"agent:triage","disabled":false}' ...
```

**停用优先级建议**（先保核心）：`followup` → `intake` → `triage` → `booking`。挂号是主链路，最后才考虑。

### 4.2 熔断复位

见 [3.6](#36-medicalagentbreakeropenwarning)——**确认依赖恢复后再复位**。

### 4.3 回滚

```bash
# 查看发布历史
helm history medical -n medical-<env>

# 回滚到上一版本（helm 自带，最快）
helm rollback medical -n medical-<env>

# 或回滚到指定镜像 SHA（更精确，推荐——镜像 tag 就是 git sha）
helm upgrade medical charts/medical-agent \
  -f charts/medical-agent/values-<env>.yaml \
  --set image.tag=<上一版 git sha> \
  -n medical-<env> --atomic --timeout 5m --wait

kubectl -n medical-<env> rollout status deploy/medical-medical-agent --timeout=300s
```

> **发版前记下当前 SHA**，回滚时才有目标：`kubectl -n medical-<env> get deploy medical-medical-agent -o jsonpath='{.spec.template.spec.containers[0].image}'`

### 4.4 扩容

```bash
# 临时调副本（HPA 开启时会被覆盖，需先确认）
kubectl -n medical-<env> scale deploy/medical-medical-agent --replicas=<N>

# 持久调整：改 values-<env>.yaml 的 autoscaling.maxReplicas 后走 CD
```

### 4.5 单院区隔离

某院区数据/配置出问题时，可只影响该租户而不牵连全站：

```bash
# 查看租户
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/api/admin/tenants | jq

# 新建/调整科室归属（谨慎，会影响该院区全部用户）
# POST /api/admin/departments  {"code","name","tenant_id"}
```

### 4.6 数据合规操作

```bash
# 留存清理（超期 PHI 按策略清理）
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" https://<host>/api/admin/retention

# 患者行使删除权（整体抹除 + 审计假名化）——
# 不可逆，必须核验申请人身份并留工单
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"username":"<patient>"}' https://<host>/api/admin/erase
```

> **删除权操作务必双人复核**：不可逆，且涉及法定合规义务。

### 4.7 备份与恢复演练

> 没演练过恢复的备份，在事故中等于没有备份。

#### 备份

```bash
# 每日/每次发版前执行，输出到 NFS/对象存储挂载点
python scripts/backup.py --out /mnt/nfs/medical-agent-backup

# 输出结构
# /mnt/nfs/medical-agent-backup/2026-08-30_123456/
#   manifest.json       # 校验和与元数据
#   postgres.sql.gz     # 主库
#   data/               # 每患者 SQLite PHI
#   env/.env.b64        # 环境配置（需另行加密保管密钥）
```

#### 恢复演练（临时库，不污染生产）

```bash
BACKUP_DIR=/mnt/nfs/medical-agent-backup/2026-08-30_123456

# 1) 校验备份完整性 + 恢复到临时库 + 健康查询
DATABASE_URL=postgresql+psycopg2://mac@localhost:5432/medical_agent \
  python scripts/restore.py --backup "$BACKUP_DIR" --verify

# 2) 若演练通过，记录到 drill log（见 4.7.1）
```

#### 真实灾难恢复（覆盖目标库，危险）

```bash
# 必须先 drop/create 目标库，再恢复
DATABASE_URL=postgresql+psycopg2://mac@localhost:5432/medical_agent \
  python scripts/restore.py --backup "$BACKUP_DIR" \
  --target-db postgresql+psycopg2://mac@localhost:5432/medical_agent --yes
```

> **注意**：恢复后必须重启应用 Pod，让 SQLAlchemy 连接池重新建立；并验证 `/health` 与一条样例对话。

#### 4.7.1 演练记录（每次演练后追加）

```markdown
- 2026-08-30 01:24 CST
  - 执行人：workbench-agent
  - 备份：/tmp/medical-agent-backup-test3/2026-08-29_212442
  - 临时库：medical_agent_restore_drill_91248
  - 健康查询：users=59, doctors=3, appointments=2, tenants=1
  - SQLite：204 个私有库成功恢复
  - 结果：通过
  - 发现问题：初始脚本含 --create 导致恢复目标库名冲突，已修复（去掉 --create）
```

### 4.8 验证告警闭环是否还活着

告警配置正确 ≠ 告警能送达。每次改完 Alertmanager 配置后都应验证：

```bash
# 发一条测试告警
curl -XPOST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[
  {"labels":{"alertname":"SmokeTest","severity":"critical","instance":"local"},
   "annotations":{"summary":"告警闭环冒烟测试"}}]'

# 确认接收端收到
docker compose -f observability/docker-compose.yml logs -f alert-sink
cat observability/alert-sink-data/alerts.jsonl
```

### 4.9 按「问题编号」定位单轮对话

患者/客服报障时，前端每条 AI 回复下方都展示了**问题编号**（即 `trace_id`，SSE 首帧 `meta` 事件下发）。
拿到这个编号就能直达该轮全量上下文，不必再靠「大概几点 + 用户名」翻日志。

```bash
# ① 按问题编号查该轮完整审计记录（含模型 / prompt 版本 / token / 状态 / 输入输出）
curl -fsS -H "Authorization: Bearer $DOCTOR_TOKEN" \
  "$HOST/api/trace/<trace_id>" | jq

# ② 返回里的 otel_trace_id 是 32 位 W3C 格式，可直接粘进 Jaeger / Grafana Tempo
#    定位完整的 supervisor → agent → tool / llm 调用链

# ③ 按患者 / 会话 / 状态批量筛（例如只看超时）
curl -fsS -H "Authorization: Bearer $DOCTOR_TOKEN" \
  "$HOST/api/chat-logs?patient_id=alice&limit=50" | jq '.logs[] | {trace_id,status,latency_ms}'

curl -fsS -H "Authorization: Bearer $DOCTOR_TOKEN" \
  "$HOST/api/chat-logs?status=timeout&limit=50" | jq '.logs[].trace_id'
```

**审计记录里 `status` 的含义**（排障时先看这一列）：

| status | 含义 | 通常下一步 |
|---|---|---|
| `ok` | 正常完成 | — |
| `timeout` | 超过 `CHAT_TIMEOUT_SECONDS` 未产出有效 token | 查 LLM 延迟 / 熔断（3.2 / 3.6） |
| `guard` | 输出侧护栏拦截（模型说了诊断/处方/剂量） | 查护栏命中样本，评估是否需调 prompt |
| `gate` | Tier-0 硬闸拦截（急症 / 定位违规 / 未签同意） | 正常行为，无需处理 |
| `degraded` | 安全降级（依赖不可用或预算耗尽） | 查熔断 / kill switch / 成本预算 |
| `pending` | 已挂起等待人工审批 | 查审批积压（3.5） |

> **排查 prompt 灰度问题时必看 `prompt_version` 列**：出问题的往往正是灰度中的 v2，
> 没有这一列你根本判断不了是哪个版本的锅。

### 4.10 成本预算熔断与解除

```bash
# 查看消耗与预算状态（budget.exceeded=true 表示已触发熔断）
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" "$HOST/api/admin/cost" | jq '.budget'

# 确认是误触（而非真的失控）后手动解除：清空当日预算计数器
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" "$HOST/api/admin/cost?reset=1" | jq '.budget'

# 被拒绝的调用数（突增说明可能有失控循环或滥用）
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" "$HOST/metrics" | grep llm_budget_blocks
```

**配置**（`.env`，0 表示不限制）：

- `LLM_DAILY_TOKEN_BUDGET` —— 全局单日 token 上限
- `LLM_PER_PATIENT_DAILY_TOKEN_BUDGET` —— 单患者单日上限（**不连坐**其他患者）

> ⚠️ 预算计数器是**进程内**的：多副本部署时各副本独立计数，重启清零。
> 生产若需严格全局限流，应改用 Redis 原子计数（`INCRBY` + 过期键）。

---

## 5. 升级矩阵

> **模板**：实际部署时请把角色替换为具体值班人与联系方式，并接入企业 IM / 电话告警（生产配置见 `observability/alertmanager/alertmanager.production.yml`，用 `url_file` 引用密钥）。

| 场景 | 一级响应 | 升级到 | 升级触发条件 |
|---|---|---|---|
| 患者完全无法使用（critical） | 值班运维 | 应用负责人 → 技术负责人 | 15 分钟未恢复 |
| 安全闸突增 / 疑似攻击 | 值班运维 | **安全负责人（立即）** + 应用负责人 | 确认是攻击即升级，不等 15 分钟 |
| 数据疑似泄漏 / 越权 | 值班运维 | **安全负责人 + 合规负责人（立即）** | 发现即升级 |
| LLM 长时间不可用 | 值班运维 | 应用负责人 | 30 分钟未恢复 |
| 审批积压 | 值班运维 | 科室排班负责人 | 积压 > 50 单或持续 1 小时 |
| 依赖方（HIS/LIS）故障 | 值班运维 | 依赖方对接人 | 确认是对方问题即通知 |

### 事故记录模板（事后必须补）

```
【事故时间线】
HH:MM  告警触发（哪条、什么值）
HH:MM  值班人确认
HH:MM  止损动作（回滚 / kill switch / 扩容）
HH:MM  服务恢复
HH:MM  根因定位

【影响面】 持续时间 / 受影响患者数或请求数 / 是否影响挂号主链路
【根因】
【为什么没被更早发现】（监控盲区？阈值太松？）
【改进项】 负责人 + 截止时间
```

> 最后两栏是复盘的价值所在。**没有改进项的事故报告等于没复盘。**

---

## 6. 命令速查表

### 环境变量（先设好）

```bash
export ADMIN_API_KEY=<你的 ADMIN_API_KEY>
export HOST=https://<host>
export ENV=prod            # dev / staging / prod
export NS=medical-$ENV
```

### 健康与状态

```bash
curl -fsS $HOST/health                                              # 存活
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" $HOST/metrics            # 指标
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" $HOST/api/admin/resilience | jq   # 熔断+killswitch
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" $HOST/api/admin/cost | jq         # LLM 成本
```

### 处置动作

```bash
# kill switch 停用/恢复
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"target":"<tool|agent:x>","disabled":true}' $HOST/api/admin/killswitch

# 熔断复位（确认依赖已恢复！）
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"<breaker>"}' $HOST/api/admin/breaker/reset

# 查看待审批积压
curl -fsS -H "Authorization: Bearer $DOCTOR_TOKEN" $HOST/api/review/pending | jq 'length'
```

### K8s 与发布

```bash
kubectl -n $NS get pods -l app.kubernetes.io/name=medical-agent
kubectl -n $NS logs -l app.kubernetes.io/name=medical-agent --tail=200
kubectl -n $NS describe pod <pod>                    # 看 CrashLoop / OOM 原因
kubectl -n $NS get deploy medical-medical-agent -o jsonpath='{.spec.template.spec.containers[0].image}'

helm history medical -n $NS                          # 发布历史
helm rollback medical -n $NS                         # 回滚上一版
helm upgrade medical charts/medical-agent -f charts/medical-agent/values-$ENV.yaml \
  --set image.tag=<sha> -n $NS --atomic --timeout 5m --wait
```

### 可观测栈

| 服务 | 地址 | 用途 |
|---|---|---|
| Prometheus | `:9090` | 指标查询、告警规则状态 |
| Grafana | `:3000` | 医疗 Agent 可观测面板 |
| Alertmanager | `:9093` | 告警路由、静默 |
| Alert Sink | `:9101/health` | 本地告警接收端（验证闭环） |

```bash
cd observability && docker compose up -d
docker compose logs -f alert-sink            # 看告警是否真的送达
cat alert-sink-data/alerts.jsonl             # 告警落盘证据（每行一条 JSON）
```

---

## 附：本手册未覆盖的部分（诚实说明）

以下场景在本项目中**尚无标准处置流程**，需在真实运行后补充：

1. **数据库主备切换** —— 项目用托管 RDS/PolarDB，切换流程依赖云厂商能力，未在此定义。
2. **多院区容灾切换** —— 多租户已实现数据隔离，但跨院区流量切换尚无预案。
3. **LLM 供应商切换** —— 支持 `fake`/`ollama`/`openai` 三种模式，但运行时热切换的验证流程未定义。
4. **值班表与联系方式** —— 第 5 节为模板，需填入真实值班人与企业 IM/电话告警。

> 备份恢复演练已在 4.7 节实现并记录一次演练结果，建议至少每季度重复一次。
