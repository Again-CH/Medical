# 医疗预约诊疗 Agent（LangGraph 脚手架）

[![CI](https://github.com/<your-org>/<your-repo>/actions/workflows/ci.yml/badge.svg)](https://github.com/<your-org>/<your-repo>/actions/workflows/ci.yml)

> 把上面徽章里的 `<your-org>/<your-repo>` 替换成你自己的 GitHub 仓库路径即可。

基于链路图落地的**可运行**脚手架：中枢辐射（hub-and-spoke）编排 + 人工审核门（Human-in-the-Loop）+ 合规横切层，技术栈 **LangGraph + FastAPI**。

默认 `LLM_MODE=fake`，**无需任何 API key 即可端到端跑通**；可一键切换到 `ollama` / `openai` / `qwen`。

## 目录结构

```
Medical-care/
├── src/
│   ├── config.py          # 模型/安全配置（env 加载）
│   ├── llm.py             # 模型适配层：fake / ollama / openai / qwen
│   ├── redline.py         # 红线引擎（急症/违规词库，AI 不下诊断）
│   ├── memory.py          # 患者长期记忆
│   ├── store.py           # 审批存储 + 审计（Memory/Json/Postgres 可插拔）
│   ├── kb.py              # RAG 知识库（SNOMED/Milvus 替换点）
│   ├── state.py           # LangGraph AgentState
│   ├── state_utils.py     # 取最后一条用户消息等工具
│   ├── supervisor.py      # 编排中枢：红线检测 + 意图分类
│   ├── agents.py          # 5 个子 Agent 节点（bind_tools ReAct）+ final_answer
│   ├── graph.py           # StateGraph 编排（interrupt / Command / checkpointer）
│   ├── gateway.py         # FastAPI：/api/chat(SSE) / review / audit
│   └── tools/             # 每个子 Agent 独立工具命名空间
│       ├── triage.py      #   search_department / dept_map(RAG)
│       ├── booking.py     #   query_availability / lock_appointment / medicare_settle
│       ├── intake.py      #   read_lab_report(LIS) / clinical_kb(RAG)
│       ├── followup.py    #   read_vitals / plan_reminder / memory_append
│       ├── emergency.py   #   handoff / call_120
│       └── __init__.py    #   NAMESPACES 注册表
├── client/
│   ├── chat.html          # 患者端（SSE 流式消费）
│   └── review.html        # 医护端（待审列表 + 批准/拒绝）
├── tests/
│   ├── test_graph.py      # 端到端 smoke：分诊 / 挂号 interrupt-resume / 红线急诊
│   └── eval/
│       ├── redline_cases.json   # 红线命中评测集
│       ├── intent_cases.json    # 意图分类评测集
│       └── test_eval.py         # pytest 跑评测集
├── requirements.txt
├── docker-compose.yml     # 可选：PostgreSQL 持久化依赖
└── .env.example
```

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 默认 fake 模式即可运行

# 启动网关
uvicorn src.gateway:app --reload --port 8000

# 另开终端，浏览器打开
#   患者端：client/chat.html
#   医护端：client/review.html
```

## 跑测试 / 评测

```bash
pytest -q                 # 端到端 smoke + 红线/意图评测集
```

## 主链路（一次请求）

```
患者端 --(SSE)--> FastAPI 网关 --RBAC--> Supervisor
  Supervisor: ① 红线检测 → 命中急症走 emergency
              ② 意图分类 → triage/booking/intake/followup/emergency
  子 Agent 用 bind_tools 让 LLM 自主选定「各自命名空间」工具并执行（ToolNode 范式）
  booking / emergency 触发 interrupt() 暂停 → 人工审核门
  医生 review.html 批准 → Command(resume) 继续 → 锁号/结算/转诊
  final_answer → 经 graph.astream_events 直接流式 LLM token → SSE 推送患者端
```

## 切换模型

编辑 `.env` 的 `LLM_MODE`：

- `fake`：确定性假模型（继承自 `BaseChatModel`，支持真·token 流式），开箱即跑（演示/测试默认）。
- `ollama`：本机 `ollama run qwen2.5:7b`，数据不出本机。
- `openai` / `qwen`：走 OpenAI 兼容接口，Qwen 可私有化部署满足数据不出域。

## 工程化增强（对应链路图 ④）

- **流式对话（直连 LLM token）**：`/api/chat` 用 `graph.astream_events` 捕获 `on_chat_model_stream` 事件，把 **LLM 真实生成的 token** 实时经 SSE 推送到患者端（fake 模式由 `FakeLLM` 以 `BaseChatModel` 流式产出，效果一致）。
- **评测集 eval**：`tests/eval` 含红线/意图小数据集，pytest 可现场跑，CI 守红线。
- **Fallback 降级**：LLM 不可用时不阻断——fake 模式即兜底；敏感路径仍走 interrupt 人工。
- **多租户隔离**：`patient_id` / 后续可扩展 `hospital_id` / `dept_id`，checkpointer/store/记忆按租户分片，RBAC 叠加作用域。
- **审批持久化（Postgres）**：设置 `DATABASE_URL` 后，审批单与审计日志自动落地 PostgreSQL（SQLAlchemy Core，建表自动）；未设置则回退内存/JSON 文件，依旧开箱即跑。
- **工具调用（bind_tools 函数调用）**：子 Agent 通过 `llm.bind_tools(本命名空间工具)` 让 LLM **自主决定**调用哪些工具，执行结果回填 `ToolMessage` 再汇总；敏感动作（锁号/结算/转诊/120）在执行前触发 `interrupt()` 人工审核门。fake 模式由 `FakeLLM` 确定性返回该命名空间的 `tool_calls`，无需 API 即可演示完整 ReAct。
- **LangSmith 链路追踪 + 离线评测**：设 `LANGSMITH_TRACING=true` 后 langgraph 运行自动上报；`scripts/eval_offline.py` 复用 `tests/eval` 数据集批量端到端评测，输出红线/意图准确率报告（可一并上报 LangSmith）。

### 启用 PostgreSQL 持久化（可选，生产推荐）

```bash
# 1) 起 Postgres（docker 已就绪）
docker compose up -d

# 2) 配置连接串（写入 .env）
export DATABASE_URL=postgresql://med_user:med_pass@localhost:5432/med_agent

# 3) 安装驱动并启动
pip install sqlalchemy psycopg2-binary
uvicorn src.gateway:app --reload --port 8000
```

> 说明：SQLAlchemy / psycopg 为 **lazy import**，仅在 `DATABASE_URL` 设置时才需安装，脚手架默认（内存/JSON）模式无需这两个依赖即可运行。

## 离线评测（评测集 + LangSmith 联动）

```bash
# 本地批量评测（红线 + 意图），输出准确率报告
python scripts/eval_offline.py

# 开启 LangSmith 链路追踪后，graph 运行自动上报，评测与追踪联动
LANGSMITH_TRACING=true LANGSMITH_API_KEY=ls-xxx python scripts/eval_offline.py
```

## 生产升级建议（已落地部分见上）

- 多租户按 `hospital_id`/`dept_id` 分片（checkpointer/store/记忆按租户分片，RBAC 叠加 tenant 作用域）。

## CI 持续集成

已内置 GitHub Actions（`.github/workflows/ci.yml`），推到 GitHub 后自动运行，覆盖「代码质量 → 测试 → 评测 → 集成」四道门禁。踩坑点与复用清单见 [`docs/ci-quickref.md`](docs/ci-quickref.md)。

| Job | 作用 | 卡点 |
| --- | --- | --- |
| `lint` | `ruff check` + `ruff format --check` | 代码风格 / 明显错误 |
| `test` | `pytest`（Python 3.11 / 3.12 / 3.13 矩阵） | 端到端 smoke + 红线/意图评测集 + 网关/存储单测 |
| `eval` | `scripts/eval_offline.py` | 红线 / 意图准确率（评测集即守门员），报告作为 artifact 上传 |
| `integration` | 起 PostgreSQL 16 服务容器 | `tests/test_store.py` 真实持久化往返 |

本地复刻 CI 步骤：

```bash
pip install ruff && ruff check . && ruff format --check .   # ① 静态检查
pytest -q                                                 # ② 测试
python scripts/eval_offline.py --out eval_report.json     # ③ 评测门禁
DATABASE_URL=postgresql://med_user:med_pass@localhost:5432/med_agent \
  pytest tests/test_store.py -q                            # ④ 集成（需本地 Postgres）
```

> 默认 `LLM_MODE=fake` + 内存存储，CI 无需任何 API key 即可全绿；评测与 PostgreSQL 为可插拔增强。
