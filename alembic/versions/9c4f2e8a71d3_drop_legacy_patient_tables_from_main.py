"""删除主库中遗留的患者私有表，恢复「PHI 物理隔离」不变量。

背景
----
初始迁移 ``21d137bd21a7`` 建表时，患者私有模型（lab_reports / vital_signs /
conversation_memory / reminders / emergency_events）尚挂在共享 ``Base`` 下，
于是这 5 张表被建进了**共享主库**，并带 ``patient_id INTEGER`` 外键指向 users.id。

架构演进为「每患者一个独立 SQLite（PatientBase）」后，私有表不再进主库，但历史表
从未清理，造成两个真实后果：

1. **合规**：PHI 实际存放在共享主库，与项目宣称的「PHI 物理隔离」直接矛盾；
2. **功能**：主库遗留表的 FK 指向 users.id，导致「删除权」执行 DELETE FROM users
   时被外键拦下（``conversation_memory_patient_id_fkey`` 违反），擦除流程在
   Postgres 上失败。

前置条件
--------
**必须先执行** ``scripts/migrate_phi_to_private_dbs.py`` 把遗留行迁入
``data/<username>.db``（走 PatientBase 模型，自动获得 EncryptedText 加密），
核对无误后再跑本迁移。本迁移只删空壳/已迁出的表，不做数据搬运。

为何新增迁移而不改初始迁移：初始迁移已在存量库上应用过，修改历史迁移会导致
版本不一致。建后即删是标准的演进写法。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9c4f2e8a71d3"
down_revision: Union[str, None] = "262ab07cc03b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 遗留私有表（均外键依赖 users，彼此间无依赖，删除顺序无约束）
LEGACY_TABLES = (
    "conversation_memory",
    "lab_reports",
    "vital_signs",
    "reminders",
    "emergency_events",
)


def _existing_tables() -> set:
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    for table in LEGACY_TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    """重建遗留表结构（仅用于回滚，数据不会自动回填 —— 数据在 per-patient SQLite）。

    回滚后需重新执行 ``scripts/migrate_phi_to_private_dbs.py`` 的逆向流程
    （手工把私有库数据写回主库）才能恢复数据；这也是「PHI 不入主库」的应有代价。
    """
    op.create_table(
        "emergency_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("remind_at", sa.String(length=19), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "vital_signs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("value", sa.String(length=32), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("measured_at", sa.String(length=19), nullable=True),
    )
    op.create_table(
        "lab_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("item", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=64), nullable=True),
        sa.Column("ref_range", sa.String(length=64), nullable=True),
        sa.Column("abnormal", sa.Boolean(), nullable=True),
        sa.Column("report_date", sa.String(length=10), nullable=True),
    )
    op.create_table(
        "conversation_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("key", sa.String(length=64), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
