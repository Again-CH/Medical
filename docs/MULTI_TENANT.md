# 多租户（多院区）支持 · MULTI_TENANT

> 范围：科室主数据（`departments` / `symptom_dept_map`）按 `tenant_id` 隔离。
> 这是医疗信息化「多院区 / 集团化部署」的刚需，也是原 P2 清单里「从能跑到能交付运维」的最后一块拼图之一。

## 1. 数据模型

- 新增 `tenants` 表：`id, code, name, is_default, created_at`。
- **`tenant_id`（`Integer FK → tenants.id`，`nullable=False`）覆盖范围**：

  | 阶段 | 表 | 说明 |
  |---|---|---|
  | ① 科室主数据 | `departments`、`symptom_dept_map` | 迁移 `0b8ce330fa1c` |
  | ② 业务主数据 | `doctors`、`doctor_schedules`、`appointments`、`exam_steps` | 迁移 `262ab07cc03b` |

  覆盖到业务主数据后，任何一张表都能**直接按 `tenant_id` 过滤**，不必层层 JOIN 推导归属。

- **`users` 刻意不加 `tenant_id`**（建模判断，非遗漏）：患者可跨院区就诊，身份是集团内全局共享的（同一账号在 A/B 院区都能挂号）。若按租户切分账号，会导致同一患者在不同院区重复建档、病历碎片化。预约归属哪个院区由 `Appointment.tenant_id` 表达。此决策由 `tests/test_tenant.py::test_patient_identity_is_global` 固化，防止后人误加。
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

## 4.1 业务主数据迁移：`262ab07cc03b`

加列 → **派生回填** → 双方言收紧约束。回填顺序不可调换（后一层依赖前一层结果）：

```
doctors.tenant_id          ← departments.tenant_id   (via dept_id)
doctor_schedules.tenant_id ← doctors.tenant_id       (via doctor_id)
appointments.tenant_id     ← doctors.tenant_id       (via doctor_id)
exam_steps.tenant_id       ← appointments.tenant_id  (via appointment_id)
```

**关键点**：不能把存量数据一律塞进默认租户 —— 那会把属于 B 院区的历史预约错挂到默认院区。
每层派生后，对「推导不出」的孤儿行（如医生未挂科室、检查单未关联预约）兜底到默认租户。

实测（本地 Postgres）：3 位医生的 `tenant_id` 与其科室租户**完全一致（0 处不一致）**，
18 条排班、7 条检查单正确派生；`alembic check` 零漂移。

## 5. 测试覆盖

`tests/test_tenant.py`（10 项，全部转绿）：

1. `test_default_tenant_seeded` —— 默认租户（id=1）已种子；
2. `test_resolve_falls_back_to_default` —— 无上下文自动回退默认租户；
3. `test_department_scoping_isolation` —— 第二个租户下的 `腰疼→骨科E` 对默认租户**不可见**；
4. `test_departments_endpoint_header_filter` —— `X-Tenant-Id` 头驱动科室列表过滤；
5. `test_admin_tenant_endpoints` —— 401 未授权、创建租户 200、重复 409、列表、在租户下建科室；
6. `test_doctor_and_schedule_tenant_isolation` —— 医生与排班按租户隔离，默认院区查不到城南院区的医生与其号源；
7. `test_availability_is_tenant_scoped` —— 号源查询在城南上下文可见 5 个号，在默认上下文「查无此科室」；
8. `test_appointment_carries_tenant_id` —— 挂号产生的预约带正确 `tenant_id`，且锁中的是城南院区的医生；
9. `test_exam_order_tenant_isolation` —— 城南开单默认院区看不到；跨院区按 id 改单返回 404；
10. `test_patient_identity_is_global` —— 固化「`users` 无 `tenant_id`」这一建模决策。

> 用例 8、9 自建专用患者（`tenant_book_patient` / `tenant_probe_patient`），不依赖种子数据
> 与测试执行顺序 —— 全新库上 TestClient 不触发 lifespan 播种，依赖 `alice` 会导致 skip 或 404，
> 而**跳过的断言等于没测**。

## 6. 排查中发现并修复的遗留问题：主库混入了患者私有表

扩展多租户时跑 Postgres 全量回归，暴露 `test_patient_can_erase_own_data` 失败：
删除用户被外键 `conversation_memory_patient_id_fkey` 拦下。追下去发现——

**初始迁移 `21d137bd21a7` 建表时，5 张患者私有表还挂在共享 `Base` 下，被建进了主库**
（`conversation_memory` / `lab_reports` / `vital_signs` / `reminders` / `emergency_events`，
`patient_id INTEGER` 外键指向 users.id），且**里面有真实 PHI 数据**。架构演进为
「每患者独立 SQLite」后，这些历史表从未清理。两个后果：

1. **合规**：PHI 实际躺在共享主库，与项目宣称的「PHI 物理隔离」直接矛盾；
2. **功能**：遗留表的外键使「删除权」在 Postgres 上失败。

修复（两步，顺序不可颠倒）：

1. **`scripts/migrate_phi_to_private_dbs.py`**（幂等，支持 `--dry-run`）：按
   `users.id → username` 映射，把遗留行写入 `data/<username>.db`（走 PatientBase 模型，
   自动获得 EncryptedText 加密），按业务键去重。**必须先跑它核对无误**，否则会丢数据
   —— 实测中 `alice.db` 本不存在，其 PHI 只在主库有。
2. **迁移 `9c4f2e8a71d3`**：删除主库这 5 张遗留表。实测主库 26 → 21 张，`alembic check` 零漂移。

**教训**：`alembic check` 只比「ORM 模型 vs 迁移」，不会告诉你「主库里多了不该有的表」。
私有表不进主库这条不变量，此前没有任何自动化检查守住。
