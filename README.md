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

## 接本地模型（Ollama，推荐）

默认 `fake` 模式用确定性假模型，**无需任何 API key 即可演示全流程**；若要真正用 LLM 回答，推荐接 Ollama 本地模型（本机无 OpenAI key 的最佳选择）：

```bash
# 1) 安装并启动 Ollama（https://ollama.com），守护进程起来后默认监听 11434
ollama --version          # 确认已装
ollama pull qwen2.5:7b   # 推荐；机器吃力可换轻量版 qwen2.5:3b / qwen2.5:1.5b

# 2) 切到 Ollama 模式（.env 里改）
LLM_MODE=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b

# 3) 重新安装依赖（含 langchain-ollama）并启动
pip install -r requirements.txt
uvicorn src.gateway:app --reload --port 8000
```

要点：
- `src/llm.py` 的 `get_llm()` 已原生支持 `fake / ollama / openai / qwen` 四种模式，靠 `LLM_MODE` 切换。
- **优雅降级**：若 `langchain-ollama` 未装或 Ollama 服务不可达，会打印明确警告并自动回退 `fake` 模式，链路不会断。
- 意图分类在 `src/supervisor.py`：fake 走关键词（确定性、可复现）；真实模型走 LLM 结构化分类，失败回退关键词。
- 真实模型同样走 `bind_tools` 函数调用范式，敏感动作（锁号/结算/转诊/120）仍触发 `interrupt()` 人工审核门。

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

### 真实落地运行（默认即生产形态）

项目已不是 demo：设置 `DATABASE_URL` 后，**号源/预约/用户/审批/记忆全部落地真实数据库**，鉴权为 JWT 多用户，工具走可插拔的 `DbHub` 适配器。三种运行形态：

| 形态 | 触发条件 | 持久化 | 模型 | 用途 |
| --- | --- | --- | --- | --- |
| 真实生产 | `DATABASE_URL=postgresql+psycopg2://...` | 真实 PostgreSQL | Ollama / OpenAI / Qwen | 面试演示 / 部署 |
| 本地开发 | `DATABASE_URL=sqlite:///./dev.db` | 本地 SQLite（同构 SQL） | Ollama | 零依赖验证 |
| 离线 demo | 不设置 `DATABASE_URL` | 内存 MemoryHub | fake | 开箱即跑 / CI 秒过 |

**① 本地一键起（macOS + brew Postgres，无需 Docker）**

```bash
cp .env.example .env          # 默认 LLM_MODE=ollama + DATABASE_URL=本地 PG
./scripts/setup_local.sh      # 起 PG → 建库 → migrate → seed → uvicorn :8000
# 或分步：make migrate && make seed && make run
```

> **Schema 版本管理（Alembic）**：`make migrate` 与 Docker 启动都会自动执行 `alembic upgrade head`，
> 按 `alembic/versions/` 下的带版本迁移演进 schema（可回滚、可演进）。后续要改表结构，只需修改
> `src/db.py` 中的模型，再运行 `DATABASE_URL=<库> .venv/bin/alembic revision --autogenerate -m "<描述>"`
> 生成新迁移即可，无需手写 SQL。旧库（曾用 `create_all` 建表）首次启动会自动 `stamp head`，不会重复建表。

**② Docker 部署（含 Postgres + 网关）**

```bash
docker compose up -d          # postgres + app 一键起，app 启动自动 migrate
# 浏览器打开 client/chat.html（患者端）与 client/review.html（医护端）
```

**③ 鉴权（JWT 多用户）**：患者/医护分别注册登录，令牌为 JWT；审批接口仅 `doctor` 角色可调。

```bash
curl -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alice123","role":"patient"}'
```

> **接真实医院系统**：工具只依赖 `src/integrations` 的端口（Protocol）契约。要接 HIS / 医保网关 / LIS / 短信，
> 只需新增一个实现这些端口的类（如 `ApiHub`），在 `get_hub()` 里按配置切换——工具与编排代码零改动。
> 当前 `DbHub` 即为本地可跑的"真实"实现（数据落库），`MemoryHub` 为离线兜底。

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
| `integration` | 起 PostgreSQL 16 服务容器 | 全量测试跑在**真实 Postgres** 上（conftest 注入 `DATABASE_URL`） |

本地复刻 CI 步骤：

```bash
pip install ruff && ruff check . && ruff format --check .   # ① 静态检查
pytest -q                                                 # ② 测试
python scripts/eval_offline.py --out eval_report.json     # ③ 评测门禁
DATABASE_URL=postgresql://med_user:med_pass@localhost:5432/med_agent \
  pytest tests/test_store.py -q                            # ④ 集成（需本地 Postgres）
```

> 默认 `LLM_MODE=fake` + 内存存储，CI 无需任何 API key 即可全绿；评测与 PostgreSQL 为可插拔增强。
