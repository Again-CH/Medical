# 多租户（多院区）支持 · MULTI_TENANT

> 范围：科室主数据（`departments` / `symptom_dept_map`）按 `tenant_id` 隔离。
> 这是医疗信息化「多院区 / 集团化部署」的刚需，也是原 P2 清单里「从能跑到能交付运维」的最后一块拼图之一。

## 1. 数据模型

- 新增 `tenants` 表：`id, code, name, is_default, created_at`。
- `Department.tenant_id`、`SymptomDeptMap.tenant_id`：`Integer FK → tenants.id`，`nullable=False`。
- 迁移 `alembic/versions/0b8ce330fa1c_multi_tenant_departments.py`：
  - 新建 `tenants` 表，写入默认租户（`code='DEFAULT', name='默认院区', id=1, is_default=True`）；
  - 给 `departments` / `symptom_dept_map` 加 `tenant_id` 列（先 nullable），回填历史数据 `UPDATE ... SET tenant_id=1 WHERE tenant_id IS NULL`；
  - 双方言落地：Postgres 直接 `op.create_foreign_key` + `op.alter_column(not null)`，命名 FK `departments_tenant_id_fkey` / `symptom_dept_map_tenant_id_fkey`；SQLite 走 `op.batch_alter_table`（SQLite 不支持直接 ALTER 约束）。

**向后兼容**：所有历史 / 未指定租户的数据都归入默认租户（id=1），「加租户维度」对既有系统零改造即可继续跑。`src/seed.py` 把默认租户 id 注入到 `Department` / `SymptomDeptMap` 的种子行。

## 2. 租户解析（contextvars 传播）

`src/tenant.py` 提供：

- `resolve_tenant_id(override=None)`：最终租户 = **显式覆盖 > 上下文变量 > 默认租户**。所有触碰 `Department` / `SymptomDeptMap` 的读路径（分诊科室检索、号源查询、挂号锁号）都经此函数，自动获得隔离。
- `set_tenant_context(tid)` / `current_tenant_id()` / `default_tenant_id()`：上下文读写；DB 不可用时回退约定值 `1`。

**为什么用 contextvars**：LangGraph `ToolNode` 在独立异步任务里执行工具，无法从工具入参取租户。在请求入口（FastAPI 依赖 `require_tenant_context`）把租户写入 `tenant_ctx`，工具内部 `resolve_tenant_id()` 无感读取，工具签名无需改动，FakeLLM / 评测路径也不受影响。`src/agents.py` 的 agent 节点也会从 graph state 的 `tenant_id` 重新 `tenant_ctx.set(...)`，以跨 LangGraph 任务边界存活。

## 3. 安全约束（与 PHI 一致）

租户标识**只来自服务端上下文 / 受控请求头**（`X-Tenant-Id`）。工具 schema 中绝不含 `tenant_id` 入参 —— prompt injection 无法操纵「跨租户读取科室」。这与「工具无 `patient_id` 参数、身份只从 JWT 取」（OLP）是同一套设计哲学。

## 4. API 用法

- 受控端点：`/api/departments`、`/api/appointments/available`、`/api/admin/schedules`、`POST /api/chat` 均接受 `X-Tenant-Id` 头（缺省走默认租户）。
- 管理端点（需 `X-Admin-Key`）：
  - `POST /api/admin/tenants` 创建租户（409 兜底已存在）
  - `GET  /api/admin/tenants` 列出租户
  - `POST /api/admin/departments` 在指定 tenant 下建科室

## 5. 测试覆盖

`tests/test_tenant.py`（5 项，全部转绿）：

1. `test_default_tenant_seeded` —— 默认租户（id=1）已种子；
2. `test_resolve_falls_back_to_default` —— 无上下文自动回退默认租户；
3. `test_department_scoping_isolation` —— 第二个租户下的 `腰疼→骨科E` 对默认租户**不可见**（跨租户隔离）；
4. `test_departments_endpoint_header_filter` —— `X-Tenant-Id` 头驱动科室列表过滤；
5. `test_admin_tenant_endpoints` —— 401 未授权、创建租户 200、重复 409、列表、在租户下建科室。
