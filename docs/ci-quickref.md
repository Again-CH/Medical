# CI 持续集成速查（LangGraph 医疗 Agent 脚手架）

> 适用：`.github/workflows/ci.yml` + `pyproject.toml`。本速查聚焦**踩坑点**与**复用清单**，
> 具体 job 定义以 `ci.yml` 为准。

## 0. 四道门禁一览

| Job | 触发/依赖 | 干什么 | 失败即阻断合并？ |
|---|---|---|---|
| `lint` | 独立 | `ruff check` + `ruff format --check` | 是 |
| `test` | 独立 | `pytest` 多 Python 版本矩阵 | 是 |
| `eval` | `needs: test` | 离线评测（红线/意图），报告上传 artifact | 是（评测未过非零退出） |
| `integration` | `needs: test` | 真实 PostgreSQL 服务容器，跑持久化 | 是 |

## 1. 零门槛跑通：默认 fake + 内存

CI **不需要任何 API key** 即可全绿：

- `LLM_MODE=fake`（默认）→ `src/llm.py` 的 `FakeLLM` 提供可流式、可 `bind_tools` 的假模型。
- 审批存储默认内存 / SQLite，Postgres 仅在 `DATABASE_URL` 设置时启用（`integration` job 才注入）。

> 本地同理：`.venv/bin/python -m pytest -q` 直接过，无需 `.env`。

## 2. 评测即守门员（关键）

`scripts/eval_offline.py` 在**红线或意图任一用例未过**时 `sys.exit(1)`：

```python
if redline_acc < 1.0 or intent_acc < 1.0:
    sys.exit(1)
```

CI 的 `eval` job 因此能**真正卡质量**，不是走个过场。评测数据在 `tests/eval/redline_cases.json`、`intent_cases.json`。

## 3. 存储双覆盖（可插拔）

`tests/test_store.py` 一份用例覆盖三层，靠环境变量切换：

| 模式 | 触发 | 用途 |
|---|---|---|
| 内存 | 默认 | CI 主流程，无需 DB |
| SQLAlchemy Core（sqlite） | `APPROVAL_STORE=sqlite:///...` | 本地验证 SQL 可移植性 |
| 真实 Postgres | `DATABASE_URL=postgresql://...` | `integration` job 服务容器 |

> 关键：存储层用 **SQLAlchemy Core（裸 SQL）** 而非 ORM，sqlite 与 PG 的 DDL/DML 差异最小，本地 sqlite 验证即可覆盖大部分逻辑。

## 4. ruff 配置踩坑

- **固定版本**：`pip install "ruff==0.16.5"`。代理拉二进制很慢，固定版本避免本地与 CI 格式规则漂移导致本地过、CI 挂。
- **`select` 去 `UP`**：曾用 `pyupgrade` 会对 `List`/`Optional`/`Dict` 类型注解误报（推荐用 `list`/`X | None`）。本项目保留 `typing` 风格，故 `select = [E, F, I, W, B]`，不含 `UP`。
- **E402 处理**：测试文件在 `sys.path.insert` 之后才 `from src... import`，ruff 报 E402，已在导入行加 `# noqa: E402`（或改成 `conftest.py` 注入路径更干净）。
- **`sys.path` 注入**：测试靠 `ROOT = os.path.dirname(...)` + `sys.path.insert(0, ROOT)` 找 `src` 包；更稳的做法是仓库根放 `conftest.py` 或 `pytest` 配 `pythonpath = ["."]`（本项目未用，已在 `pyproject` 留空）。

## 5. GitHub Actions 推送提示

本沙箱环境：**github.com 的 git 协议经代理返回 502**，`git clone/push` 走代理必失败。已本地 `git init` + 首次提交。推到 GitHub 由你手动执行：

```bash
git remote add origin <你的仓库地址>
git push -u origin main   # 分支名见下方约定
```

README 顶部徽章把 `<your-org>/<your-repo>` 换成实际路径。

## 6. 复用清单（新项目套用）

1. `pyproject.toml`：ruff（E/F/I/W/B）+ pytest 配置。
2. `.github/workflows/ci.yml`：4 job 模板（见上）。
3. 把 `fake` 模型 + 内存存储作为**默认**，让 CI 零外部依赖。
4. 评测脚本务必在失败时**非零退出**，否则门禁形同虚设。
5. DB 相关测试用 SQLAlchemy Core + sqlite 做本地等价验证，真实 PG 用 service container。
6. ruff 锁版本；CI 失败先查是不是本地 ruff 版本漂移。

## 7. 本地验证命令

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
.venv/bin/python scripts/eval_offline.py --out eval_report.json
```
