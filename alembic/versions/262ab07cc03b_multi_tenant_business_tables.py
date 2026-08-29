"""multi_tenant_business_tables：把租户维度从「科室主数据」扩展到「业务主数据」。

覆盖 ``doctors`` / ``doctor_schedules`` / ``appointments`` / ``exam_steps`` 四张表，
使任何一张业务表都能直接按 ``tenant_id`` 过滤，而不必层层 JOIN 推导归属。

**回填策略（关键）**：不能把存量数据一律塞进默认租户——那会把属于 B 院区的历史
预约错挂到默认院区。改为按既有外键关系**逐层派生**：

    doctors.tenant_id          ← departments.tenant_id   (via dept_id)
    doctor_schedules.tenant_id ← doctors.tenant_id       (via doctor_id)
    appointments.tenant_id     ← doctors.tenant_id       (via doctor_id)
    exam_steps.tenant_id       ← appointments.tenant_id  (via appointment_id)

每层派生后对「推导不出」的孤儿行兜底到默认租户（id=1，由 0b8ce330fa1c 写入）。
回填顺序不可调换：后一层依赖前一层的回填结果。

**``users`` 刻意不加 tenant_id**：患者可跨院区就诊，身份是集团内全局共享的；
预约归属哪个院区由 ``Appointment.tenant_id`` 表达。这是建模判断而非遗漏。

方言适配：Postgres 直接加 FK + NOT NULL；SQLite 不支持直接 ALTER 约束，走 batch 模式。
FK 显式命名以保证两种方言一致（batch 模式要求约束必须有名字）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "262ab07cc03b"
down_revision: Union[str, None] = "0b8ce330fa1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 默认租户 id：与 0b8ce330fa1c 写入的 tenants 行保持一致，仅用于孤儿行兜底。
_DEFAULT_TENANT_ID = 1

# (表名, 派生 SQL) —— 顺序敏感，逐层依赖上一层回填结果。
_BACKFILLS = (
    (
        "doctors",
        "UPDATE doctors SET tenant_id = "
        "(SELECT d.tenant_id FROM departments d WHERE d.id = doctors.dept_id) "
        "WHERE tenant_id IS NULL",
    ),
    (
        "doctor_schedules",
        "UPDATE doctor_schedules SET tenant_id = "
        "(SELECT dc.tenant_id FROM doctors dc WHERE dc.id = doctor_schedules.doctor_id) "
        "WHERE tenant_id IS NULL",
    ),
    (
        "appointments",
        "UPDATE appointments SET tenant_id = "
        "(SELECT dc.tenant_id FROM doctors dc WHERE dc.id = appointments.doctor_id) "
        "WHERE tenant_id IS NULL",
    ),
    (
        "exam_steps",
        "UPDATE exam_steps SET tenant_id = "
        "(SELECT a.tenant_id FROM appointments a WHERE a.id = exam_steps.appointment_id) "
        "WHERE tenant_id IS NULL",
    ),
)

# (表名, 外键名)
_TABLES = (
    ("doctors", "doctors_tenant_id_fkey"),
    ("doctor_schedules", "doctor_schedules_tenant_id_fkey"),
    ("appointments", "appointments_tenant_id_fkey"),
    ("exam_steps", "exam_steps_tenant_id_fkey"),
)


def upgrade() -> None:
    # 1) 先加可空列，避免存量行因 NOT NULL 直接失败
    for table, _ in _TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.Integer(), nullable=True))

    # 2) 逐层派生回填（顺序不可调换）
    for table, sql in _BACKFILLS:
        op.execute(sa.text(sql))
        # 孤儿行（关联目标缺失，如医生未挂科室、检查单未关联预约）兜底到默认租户
        op.execute(
            sa.text(
                f"UPDATE {table} SET tenant_id = :tid WHERE tenant_id IS NULL"  # noqa: S608
            ).bindparams(tid=_DEFAULT_TENANT_ID)
        )

    # 3) 收紧约束：Postgres 直连 / SQLite batch（方言不同，故分两条路径）
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table, fk_name in _TABLES:
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_foreign_key(fk_name, "tenants", ["tenant_id"], ["id"])
                batch_op.alter_column("tenant_id", nullable=False)
    else:
        for table, fk_name in _TABLES:
            op.create_foreign_key(fk_name, table, "tenants", ["tenant_id"], ["id"])
            op.alter_column(table, "tenant_id", nullable=False)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table, fk_name in _TABLES:
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_constraint(fk_name, type_="foreignkey")
                batch_op.drop_column("tenant_id")
    else:
        for table, fk_name in _TABLES:
            op.drop_constraint(fk_name, table, type_="foreignkey")
            op.drop_column(table, "tenant_id")
