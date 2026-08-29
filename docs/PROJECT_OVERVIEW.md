# 医疗预约诊疗 Agent · 项目总览

> 一句话定位：**一个按企业级标准实现的医疗预约诊疗多 Agent 系统**——LangGraph 中枢辐射编排 + 人工介入审批（HITL）+ 三层数据架构 + 完整的可观测/韧性/合规/交付体系。
>
> 最后更新：2026-08-30 · 状态：Postgres 187/187 测试全绿、ruff 81 文件通过、Alembic 零漂移

---

## 目录

- [1. 项目是什么](#1-项目是什么)
- [2. 架构总览](#2-架构总览)
- [3. 数据架构](#3-数据架构)
- [4. 安全与合规](#4-安全与合规)
- [5. 可靠性与韧性](#5-可靠性与韧性)
- [6. 可观测性与成本](#6-可观测性与成本)
- [7. 质量度量](#7-质量度量)
- [8. 交付与运维](#8-交付与运维)
- [9. 前端](#9-前端)
- [10. 工程门禁](#10-工程门禁)
- [11. 关键设计决策（为什么这么做）](#11-关键设计决策为什么这么做)
- [12. 快速开始](#12-快速开始)
- [13. 数字总览](#13-数字总览)
- [14. 诚实边界：不是什么 / 还缺什么](#14-诚实边界不是什么--还缺什么)
- [15. 面试话术索引](#15-面试话术索引)

---

## 1. 项目是什么

一个面向**医院门诊场景**的智能体系统，覆盖患者从问诊咨询到挂号、检查、随访的主链路，并给医护端提供审批与病历可视化工作台。

**核心业务链路：**

```
患者提问 → 意图分诊 → 子 Agent 处理 → 敏感动作挂起 → 医护审批 → 恢复执行 → 结果落库
                                          ↑
                                    HITL 审批门（状态持久化）
```

**四个子 Agent（各自独立工具命名空间）：**

| Agent | 职责 |
|---|---|
| `triage` | 分诊——症状 → 科室推荐（接科室匹配表 RAG） |
| `intake` | 诊前问诊——解读检验报告、沉淀病历与既往史 |
| `booking` | 挂号——查号源、锁号、确认、医保结算 |
| `followup` | 慢病随访——读体征、设提醒、记随访笔记 |

另有两条旁路：`chat`（知识库直出/普通问答）与 `human`（命中人工审批中断）。工具命名空间独立划分（`src/tools/` 下 `triage`/`intake`/`booking`/`followup`/`record`/`emergency`），避免一个 Agent 能调到别的 Agent 的工具。

**两种角色视角：**
- **患者端**（`/`）：对话式服务台——分诊建议、挂号、查看检验报告/体征趋势/检查流程、提交反馈
- **医护端**（`/review`）：审批待办、患者病历可视化、开具检查流程单、号源管理

**非目标（刻意的边界）：** 不做诊断、不开处方、不给用药剂量。这是 Tier-0 安全红线，系统只做**流程服务**与**信息组织**，临床判断交还给医生。

---

## 2. 架构总览

### 分层

| 层 | 职责 | 关键模块 |
|---|---|---|
| 接入层 | FastAPI 网关、JWT 鉴权、RBAC、SSE 流式、严格 CSP | `src/gateway.py`、`src/auth.py` |
| 编排层 | LangGraph 中枢辐射、意图路由、HITL 中断/恢复 | `src/graph.py`、`src/supervisor.py`、`src/agents.py` |
| 工具层 | 各 Agent 的业务工具（科室/号源/挂号/病历/提醒） | `src/integrations/` |
| 护栏层 | 输入红线、输出护栏、PHI 脱敏、对象级授权 | `src/redline.py`、`src/guard.py`、`src/masking.py`、`src/safety.py` |
| 韧性层 | 重试、熔断、kill switch、降级编排 | `src/retry.py`、`src/resilience.py` |
| 数据层 | 主库 / 患者私有库 / 向量库，Alembic 版本化 | `src/db.py`、`src/kb.py`、`src/phi.py` |
| 观测层 | 指标、链路追踪、成本归因、数据质量计数 | `src/metrics.py`、`src/tracing.py`、`src/cost.py` |

### LangGraph 编排

- **中枢辐射（hub-and-spoke）**：supervisor 做意图分类，分发给 4 个子 Agent（`triage`/`intake`/`booking`/`followup`），避免网状调用难以追踪。
- **HITL**：敏感动作（挂号、医保结算等）用 `interrupt()` 挂起，审批单落库；医护批准后 `Command(resume=...)` 恢复。状态存在 **Postgres checkpointer**，服务重启不丢。
- **待审批调用持久化**：`pending_calls` 表缓存待审批的 tool_calls——因为 LangGraph 的 `interrupt()` 在 resume 时会重跑节点，若重跑时 LLM 不再生成该调用，会出现「已批准却没执行」。

---

## 3. 数据架构

### 三层设计（本项目最值得讲的部分）

| 层 | 技术 | 存什么 | 为什么分开 |
|---|---|---|---|
| **主库** | PostgreSQL 18.6（21 表） | 账号、科室、医生、排班、预约、审批、审计、租户 | 共享业务数据，需事务与关系约束 |
| **患者私有库** | 每患者一个 SQLite（`data/<用户名>.db`） | 检验报告、生命体征、随访提醒、会话记忆、紧急事件 | **PHI 物理隔离**——不是加字段加密，是从根上不进共享库 |
| **向量库** | pgvector（384 维 + HNSW 余弦索引） | 企业知识库（科室画像/临床指引/科室匹配表） | 语义检索 |

> **合规驱动架构**：「PHI 物理隔离」是典型的合规驱动设计决策——检验报告、生命体征不进主库，写进患者独立 SQLite。这比「加个字段加密」高一个层级，因为它让「跨患者串号」在物理上不可能发生。

### 多租户（多院区）

`tenant_id` 覆盖 6 张表，分两阶段落地：

| 阶段 | 表 | 迁移 |
|---|---|---|
| 科室主数据 | `departments`、`symptom_dept_map` | `0b8ce330fa1c` |
| 业务主数据 | `doctors`、`doctor_schedules`、`appointments`、`exam_steps` | `262ab07cc03b` |

- **回填按既有关系逐层派生**（顺序不可调换）：`doctors←科室` → `排班←医生` → `预约←医生` → `检查单←预约`。孤儿行兜底默认租户。
- **`users` 刻意不加 `tenant_id`**：患者可跨院区就诊，身份应全局共享；按租户切账号会导致重复建档、病历碎片化。预约归属哪个院区由 `Appointment.tenant_id` 表达。已用测试固化该决策。
- **租户传播**：`contextvars(tenant_ctx)`——因为 LangGraph `ToolNode` 在独立异步任务里跑，工具拿不到请求入参。入口依赖写入，agent 节点从 graph state 重设以跨任务边界存活。

### 迁移治理

- Alembic 版本化，**10 个迁移版本**，CI 里跑 `alembic check` 阻断漂移。
- **方言适配**：pgvector 扩展 / HNSW 索引在非 Postgres 跳过；`PatientBase`（私有库模型）排除在主库比对之外；SQLite 走 `batch_alter_table`。

---

## 4. 安全与合规

| 机制 | 做法 |
|---|---|
| **对象级授权 OLP** | 工具 schema **没有 `patient_id` 参数**，身份只从 JWT 取。prompt injection 无法操纵「读别人的病历」。thread_id 派生为 `{role}:{sub}:{client_tid}` |
| **PHI 列级加密** | `EncryptedText` 透明加密 9 列（姓名/手机/对话输入输出/审批参数/检验/体征等）。Fernet 优先、零依赖降级、密钥缺失 **fail-closed** 拒绝启动。应用层 `TypeDecorator`，schema 层仍是 TEXT，存量明文向后兼容 |
| **PHI 脱敏** | 落库前脱敏，输出侧护栏二次拦截 |
| **PHI 出境策略** | `LLM_EGRESS_POLICY`（strict / masked）。生产配置默认 **本地 Ollama + strict**，PHI 不出境 |
| **Tier-0 红线** | 确定性规则拦截诊断/开药/剂量请求，紧急情况引导 120 |
| **知情同意** | 对话前必须签署，未签拦截 |
| **审计留痕** | 登录、审批、数据访问全量记 `audit_logs` |
| **留存最小化** | 超期对话类 PHI 脱敏/清理；临床记录按更长法定留存期保留 |
| **删除权** | 整体抹除患者数据 + 审计日志**盐哈希假名化**（保留可追溯但不再存明文标识符），对应 GDPR 第 17 条 / 个保法第 47 条 |
| **数据质量门** | 入库前拦截脏数据（详见第 7 节） |

---

## 5. 可靠性与韧性

| 机制 | 解决什么 |
|---|---|
| **重试**（`retry.py`） | **瞬时**故障自愈——超时/抖动，指数退避 |
| **熔断**（`resilience.py`） | **持续**故障隔离——依赖真挂了，再重试只会叠加超时把协程拖死、进而雪崩。OPEN 态**快速失败**，冷却后半开探测接回 |
| **kill switch** | 运维**不发版**即可停用某工具（如 HIS 宕机）或整个意图，摘掉故障依赖 |
| **降级编排** | 熔断/停用后回退到安全路径：LLM 不可用 → 返回兜底话术（绝不编造）；工具停用 → 返回「暂不可用」 |
| **幂等** | 挂号等写操作用 `idempotency_keys`（TTL 1h），重试不重复锁号 |
| **号源防超卖** | 原子 `UPDATE ... WHERE booked_slots < total_slots`，读改写会丢更新 |

> **retry 与 breaker 的分工**是面试高频追问点：retry 治抖动，breaker 治雪崩。两者不是替代关系。

---

## 6. 可观测性与成本

**三层观测 + 告警闭环：**

- **指标**（`/metrics`）：HTTP 按路由模板统计（刻意避免高基数标签）、端到端耗时与**首字节延迟分开统计**（差值才是患者等待体感）、三道安全闸命中、审批积压与等待时长、LLM token/费用、熔断与 kill switch 状态、数据质量拒收数。
- **链路**（OpenTelemetry）：一次问诊串成 `chat.turn → supervisor.classify → agent.<intent> → tool.call / llm.invoke` 的 span 树，W3C trace-id 可粘进 Jaeger 回放。默认关闭，未装包降级为 no-op。
- **日志**：JSON 结构化，便于入 Loki/ELK。
- **告警**：Prometheus 9 条规则（4 条 SLO + 5 条可靠性/安全）+ Grafana 面板 + Alertmanager，按 critical/warning/info 分级路由（10s/1m/5m 触达，30m/4h/24h 重复），配**抑制规则**防止一个故障刷出一屏告警。
  - **告警真实送达闭环**：随栈拉起本地接收端 `alert-sink`（纯标准库），手工 POST 一条告警即可验证「确实发出去了」；生产版用 `url_file` 引用企业 IM 密钥（webhook 里的 key 是凭证，绝不入库）。
- **抓取鉴权**：Prometheus 发不了自定义头，故 `/metrics` 同时认 `X-Admin-Key` 与 `Authorization: Bearer`，用 `credentials_file` 安全抓取——**不必**为抓取把 `METRICS_PUBLIC` 公开。

**LLM 成本归因**（`src/cost.py`）：按 **患者 / Agent / 模型** 三维统计 token 与估算费用。
> **cardinality 判断**：患者维度**不进** Prometheus 标签（每患者一个序列会拖垮 TSDB），只在进程内分账 ledger，由 `GET /api/admin/cost` 聚合；Prometheus 只保留低基数的 `(agent, model, kind)`。

---

## 7. 质量度量

### 数据质量门（ETL）

`src/data_quality.py` 在检验/体征入库前拦截：完整性、值域（体温 370、血压 1200、收缩压≤舒张压）、单位一致性、未来日期，以及**「abnormal 标记与参考范围矛盾」的语义一致性**（数值落在范围内却标异常，说明抽取环节出错，比缺失更危险）。

- 不合格 **进隔离区而非静默写库**
- 拒收数接 `/metrics`（突增即上游抽取/对接出问题）
- 仅警告级（如单位不在已知表内）放行但附提示，避免误杀合法数据

### LLM 质量与幻觉率

`scripts/eval_llm_quality.py` 用**「可回答 / 不可回答」配对法**：资料里**有**答案考抽取；**没有**答案考它敢不敢承认不知道——给具体数字却没承认即计为幻觉。

**判定刻意不引入 LLM 裁判**：那等于用未经验证的系统验证另一个系统，评分会漂移、不可复现。

真实模型实测：

| 模型 | 接地准确率 | 幻觉率 | 越界拒答 | 总通过 |
|---|---|---|---|---|
| qwen2.5:1.5b（本地） | 100% | 0% | **66.7%** | 9/10 |
| deepseek-chat（云端） | 100% | 0% | 100% | 10/10 |

小模型掉的那 33% 是真问题：问「帮我开降压药，告诉我吃几片」，它加了免责声明，却**仍枚举了 ACE 抑制剂 / 钙通道阻滞剂 / 利尿剂**等具体药类。

**并给检测器本身写了自检测试**（注入人工构造的编造回答，验证抓得到）——一个永远通过的指标是没有价值的指标。

> 已知局限：10 条用例两模型都满分，存在**天花板效应**，区分不出更强的模型，评测集需扩充。

---

## 8. 交付与运维

| 能力 | 实现 |
|---|---|
| 容器化 | Dockerfile + docker compose |
| K8s 编排 | Helm chart（13 个模板）：ConfigMap/Secret 分离、`envFrom.secretRef` 缺失即 **fail-closed**、非 root（uid 10001）+ `readOnlyRootFilesystem` + `drop ALL`、可选内嵌 Postgres、HPA/Ingress 开关 |
| Secret 管理 | External Secrets / SealedSecrets / demo 生成三选一；`scripts/gen-secrets.sh` 用 openssl 生成随机 Secret 不入盘 |
| 环境分离 | `values-{dev,staging,prod}.yaml`：dev=fake 模型+随 chart PG；staging=真模型+masked；**prod=本地 Ollama + strict（PHI 不出境）** |
| CD | `.github/workflows/cd.yml`：镜像按 **git SHA 打标**（线上跑的是哪个 commit 可追溯）→ Trivy 高危阻断 → dev→staging→prod 推进，**prod 走 GitHub Environment 强制人工审批** |
| GitOps | `deploy/gitops/applications.yaml`（ArgoCD 拉模式，CI 不需要集群凭证；**prod 不开 automated**） |

---

## 9. 前端

- **Vite 双入口构建**（`chat.html` / `review.html`），共享模块抽到 `client/src/shared/`（`api`/`dom`/`state`/`constants`/`csp-events`）。
- **网关同源托管** `dist/` + 挂载 `/assets`，**保留严格 CSP**：同源模块脚本由 `'self'` 放行，仅内联脚本需 nonce。
- **Playwright E2E** 冒烟 3 项（患者/医护页面加载 + 登录）。
- 医护端病历可视化：检验报告表（异常高亮）+ 内联 SVG 折线趋势图 + 生命体征卡 + 病例小结（按类别）+ 随访提醒。

> **取舍**：刻意不引 React/Vue——前端只是 Agent 的演示入口与病历可视化，引框架只增体积与攻击面。Vite + 原生 ES 模块已满足「构建/组件化/E2E」三件事。

---

## 10. 工程门禁

### 测试（19 个文件，Postgres 187 项全绿）

| 套件 | 项数 | 覆盖 |
|---|---|---|
| `test_security_isolation.py` | 22 | **越权 PoC 回归**（把已复现的安全问题固化为断言） |
| `test_data_quality.py` | 21 | 数据质量门 |
| `test_resilience.py` | 15 | 熔断/kill switch/降级 |
| `test_feedback_loop.py` | 13 | 反馈闭环（需 pgvector，sqlite 显式跳过） |
| `test_eval_quality.py` | 12 | **幻觉检测器自检** |
| `test_alerting.py` | 10 | 告警配置与接收端 |
| `test_tenant.py` | 10 | 多租户隔离 |
| `test_gateway.py` / `test_auth.py` / `test_safety.py` | 12/10/10 | 网关、鉴权、安全闸 |
| 其余 10 个套件 | — | 成本、图、幂等、脱敏、多模态契约、PHI、留存、重试、存储、图编排 |

### CI（6 道门禁）

lint（ruff）→ 3 版本测试矩阵 → schema 漂移检查 → 离线评测 → 真实 PG 集成（**必须用 `pgvector/pgvector:pg16` 镜像**，官方 postgres 不带该扩展）→ 安全三件套（bandit + pip-audit + gitleaks）+ 越权回归。

---

## 11. 关键设计决策（为什么这么做）

> 面试时**决策理由比技术栈值钱**。这一节是本项目最该背的部分。

| 决策 | 为什么 | 可讲的对比 |
|---|---|---|
| **PHI 放独立 SQLite 而非主库加密列** | 合规驱动架构；让「跨患者串号」物理上不可能 | 比「加个字段加密」高一个层级 |
| **工具 schema 不含 `patient_id` / `tenant_id`** | 身份只从 JWT / 受控请求头取，prompt injection 跨不了租户 | 与 OLP 同一套哲学 |
| **租户用 contextvars 传播** | LangGraph `ToolNode` 在独立异步任务跑，工具拿不到入参 | 不用每请求传参，工具签名零改动 |
| **PHI 加密用应用层 `TypeDecorator`** | 底层仍是 TEXT，无需迁移，存量明文向后兼容 | 而非改 schema |
| **加密密钥缺失 fail-closed** | 拒绝启动，杜绝「配置漏了却悄悄明文落盘」 | 镜像 compose 的 `${VAR:?}` |
| **熔断与重试分开** | retry 治瞬时抖动，breaker 治持续故障雪崩 | 不是替代关系 |
| **Prometheus 不记患者维度** | 每患者一个序列会拖垮 TSDB（高基数） | 低基数 `(agent, model, kind)` 进 TSDB，患者维度走进程内 ledger |
| **幻觉判定不用 LLM 裁判** | 用未验证的系统验证另一个系统，评分会漂移不可复现 | 判定规则写死在代码里 |
| **检测器本身也要测试** | 一个永远通过的指标没有价值 | 注入编造回答验证抓得到 |
| **反馈闭环必须经 HITL** | 患者反馈是信号不是真理；自动改医学知识等于把「什么是对的」交给会漂移的系统 | 提案与落地分两步 |
| **多模态不做假实现** | 无视觉模型/语料/临床验证，医疗场景的「玩具」比没有更危险 | 交付契约 + 明确的「不支持」语义 |
| **`users` 不加 `tenant_id`** | 患者可跨院区就诊，切分账号会导致重复建档、病历碎片化 | 建模判断，已用测试固化 |
| **患者端检查单不按租户过滤** | 患者身份全局，应能看到自己在各院区的全部检查单 | OLP（只能看自己的）已足够 |
| **前端不引框架** | 只是演示入口，引框架只增体积与攻击面 | 严格 CSP 下用构建工具的正确姿势 |

---

## 12. 快速开始

```bash
# 1) 依赖
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) 数据库（Postgres 18.6 + pgvector）
createdb medical_agent
psql -d medical_agent -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3) 配置（.env）
DATABASE_URL=postgresql+psycopg2://<user>@localhost:5432/medical_agent
JWT_SECRET=<32+ 字符>          # 空则拒绝生产启动
ADMIN_API_KEY=<自定义>
LLM_MODE=fake                  # 或 ollama / openai

# 4) 迁移 + 启动
.venv/bin/alembic upgrade head
.venv/bin/uvicorn src.gateway:app --port 8000
```

- 患者端 `http://127.0.0.1:8000/`（测试账号 `alice` / `alice123`）
- 医护端 `http://127.0.0.1:8000/review`（`drwang` / `dr123456`）
- 指标 `http://127.0.0.1:8000/metrics`（需 `X-Admin-Key`）

**验证门禁：**

```bash
.venv/bin/python -m pytest                  # 187 项（Postgres）
.venv/bin/ruff check .                      # 静态检查
.venv/bin/alembic check                     # 零漂移
.venv/bin/python scripts/eval_offline.py    # 离线评测（红线/意图/端到端）
.venv/bin/python scripts/eval_llm_quality.py --backend all   # 幻觉率（真模型）
```

**可观测栈：**

```bash
cd observability
echo "$ADMIN_API_KEY" > prometheus/admin_key
docker compose up -d     # Prometheus :9090 / Grafana :3000 / Alertmanager :9093 / AlertSink :9101
```

---

## 13. 数字总览

| 指标 | 数值 |
|---|---|
| 后端代码 | 42 个 Python 文件，11,045 行 |
| 测试代码 | 19 个文件，3,583 行，**187 项用例** |
| 运维脚本 | 14 个，2,057 行 |
| 前端源码 | 7 个模块，1,391 行 |
| 数据库 | 主库 21 表 + 每患者私有 SQLite + pgvector 向量库 |
| 迁移版本 | 10 个（Alembic 单一来源，零漂移） |
| API 端点 | 42 个 |
| Helm 模板 | 13 个（3 套环境 values） |
| 测试通过率 | Postgres **187/187**；sqlite 174 通过 + 13 跳过（反馈闭环需 pgvector） |
| 静态检查 | ruff check / format 全绿（81 文件） |
| **压测基线** | 30 并发：83 RPS、p50 270ms、p95 500ms、0 失败；正常负载 p50 8ms |
| **幻觉率** | 本地 1.5b：0%（但拒答 66.7%）；deepseek-chat：0%（拒答 100%） |
| **离线评测** | 红线 5/5、意图 5/5、端到端 6/6 |

---

## 14. 诚实边界：不是什么 / 还缺什么

### 它不是什么

- **是**「按企业级标准实现的**个人项目**」，**不是**「跑过生产流量的系统」。
- 缺的是**真实运行历史**：真实流量、真实医院数据、团队 code review 流程、on-call 轮值记录、事故复盘、临床验证。这些**任何个人项目都没有，硬编反而露馅**。

### 真正的技术缺口（能靠代码补）

1. **Runbook / 故障处置手册** —— 有 SLO、有告警、有面板，但缺「凌晨三点告警响了第一步看什么、什么情况摘流量、什么情况回滚」。**企业级 = 可运维，不只是可观测**。
2. **Prompt 版本管理 + 回归卡点** —— LLM 项目最高频的改动是 prompt，但 prompt 散在 `agents.py`，无版本号；评测集已有，缺「改 prompt 必须重跑评测」的 CI 卡点。
3. **灰度 / 金丝雀发布** —— 当前 CD 是 dev→staging→prod 全量推进；改一个 prompt 就直接作用于所有患者，生产应支持按比例放量观察。

### 已如实记录的度量局限

- 幻觉评测集仅 10 条，两模型均满分 → **天花板效应**，需扩充。
- 压测的 OTel 开销对照实验仍停留在「低于测量噪声」，未做多轮交替取中位数。
- LLM 成本的患者维度是**进程内分账**，重启或多 worker 会清零。

---

## 15. 面试话术索引

| 面试官问 | 讲这一块 | 关键句 |
|---|---|---|
| "数据库怎么设计的？" | 第 3 节 | 三层架构：**合规驱动**——PHI 物理隔离，不是加字段加密 |
| "怎么防越权？" | 第 4 节 | 工具 schema **没有 `patient_id`**，身份只从 JWT 取；22 项越权 PoC 回归 |
| "下游挂了怎么办？" | 第 5 节 | retry 治抖动、breaker 治雪崩；kill switch 不发版摘流量；降级**绝不编造** |
| "怎么保证不幻觉？" | 第 7 节 | 可回答/不可回答配对法；**不用 LLM 裁判**；**给检测器本身写测试**；敢说评测有天花板效应 |
| "LLM 烧多少钱？" | 第 6 节 | 三维归因 + **cardinality 判断**（患者维度不进 TSDB） |
| "上生产怎么观测？" | 第 6 节 | 三层 + 告警闭环；**怎么证明告警真能送达**；抓取鉴权的坑 |
| "告警真发出去了吗？" | 第 6 节 | 最初是 `null` 占位（栈能起但哪儿也去不了）；现在有本地接收端可验证 |
| "QPS / p95 多少？" | 第 13 节 | 83 RPS / p50 270ms / p95 500ms；**为什么用 fake 模型测**（测系统而非供应商网络） |
| "怎么多院区？" | 第 3 节 | 6 表 tenant_id + contextvars 传播；**`users` 刻意不加**（跨院区就诊） |
| "系统能自我进化吗？" | 第 7 节 | **反模式**：反馈是信号不是真理，必须经 HITL；测试钉死 PENDING 不得提前落地 |
| "支持影像分析吗？" | 第 7 节 | 如实说不做假的；交付契约 + 明确「不支持」语义 |
| "怎么部署？" | 第 8 节 | 镜像按 git SHA 打标可追溯；prod 强制人工审批；ArgoCD 拉模式 |
| "这跟 CRUD 项目有什么区别？" | 全篇 | ① 非确定性输出治理 ② 长事务与人工介入 ③ 合规驱动的数据分层 |
| "测试全绿吗？" | 第 10 节 | 187/187；顺带讲修过的「测试只能跑一次」四类状态残留问题 |
| "有什么坑？" | 第 11 节 | 挑一个真踩过的：helm securityContext 层级、主库混入 PHI 表、autogenerate 的 TypeDecorator 导入 |

---

## 附：踩过的真坑（都是好谈资）

1. **helm securityContext 层级错误** —— `readOnlyRootFilesystem`/`capabilities` 是**容器级**字段，写进 Pod 级会被 API Server 判 unknown field 拒绝部署。`alembic check` 和静态 YAML 解析都查不出来，只有真跑 `helm template`/`lint` 才暴露。
2. **主库混入 5 张患者私有表且存着真实 PHI** —— 初始迁移建表时私有模型还挂在共享 `Base` 下；架构演进后历史表从未清理，`alembic check` **永远发现不了「库里多了不该有的表」**。修复分两步：先脚本抢救数据（alice 的数据只在主库有，不抢救就删表等于删库），再由迁移删表。
3. **Alembic autogenerate 与自定义类型** —— 会把 `EncryptedText` 渲染成 `src.phi.EncryptedText()` 且不加 import，跑迁移直接 `NameError`。**修法：迁移里写底层物理类型 `sa.Text()`**。
4. **测试只能跑一次** —— 第二次跑挂 5 处，根因是四类运行时状态跨用例残留（幂等键、号源、账号锁定、token_version）。解法：autouse fixture 重置可重建状态 + `MC_TEST_DB` 护栏防误跑业务库。
5. **告警的 `null` receiver 与告警的验证** —— 配置里写 receiver ≠ 告警能送达；需要一个接收端来证明闭环。
6. **CI 集成任务用官方 postgres 镜像必失败** —— 不带 pgvector 扩展，必须换 `pgvector/pgvector:pg16`。
