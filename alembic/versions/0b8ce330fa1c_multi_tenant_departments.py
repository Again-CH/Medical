"""多院区 / 多租户：departments 与 symptom_dept_map 加 tenant_id 隔离

本期把租户维度落在科室主数据层（departments / symptom_dept_map），并新建 tenants
注册表。默认租户 code='DEFAULT' 承载所有历史 / 未指定租户的数据，保证「加租户维度」
对既有系统完全向后兼容（零改造即可继续运行）。

存量回填：departments / symptom_dept_map 已有行在升级时统一归属默认租户(id=1)，
再置 NOT NULL，避免既有数据因无法写入 tenant_id 而导致升级失败。

Revision ID: 0b8ce330fa1c
Revises: g7b8c9d0e1f2
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0b8ce330fa1c"
down_revision: Union[str, None] = "g7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    # 默认租户：所有历史 / 未指定租户的数据归于此，保证向后兼容
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, code, name, is_default) "
            "VALUES (1, 'DEFAULT', '默认院区', TRUE) "
            "ON CONFLICT (code) DO NOTHING"
        )
    )

    # 先以可空列加入（存量行需回填），再统一置 NOT NULL。
    # SQLite 的 ADD COLUMN 原生支持；NOT NULL / 外键需走 batch 重建表。
    op.add_column("departments", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.add_column("symptom_dept_map", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.execute(sa.text("UPDATE departments SET tenant_id = 1 WHERE tenant_id IS NULL"))
    op.execute(sa.text("UPDATE symptom_dept_map SET tenant_id = 1 WHERE tenant_id IS NULL"))

    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.create_foreign_key(
            "departments_tenant_id_fkey", "departments", "tenants", ["tenant_id"], ["id"]
        )
        op.create_foreign_key(
            "symptom_dept_map_tenant_id_fkey",
            "symptom_dept_map",
            "tenants",
            ["tenant_id"],
            ["id"],
        )
        op.alter_column("departments", "tenant_id", existing_type=sa.Integer(), nullable=False)
        op.alter_column("symptom_dept_map", "tenant_id", existing_type=sa.Integer(), nullable=False)
    elif dialect == "sqlite":
        with op.batch_alter_table("departments") as batch:
            batch.create_foreign_key("departments_tenant_id_fkey", "tenants", ["tenant_id"], ["id"])
            batch.alter_column("tenant_id", existing_type=sa.Integer(), nullable=False)
        with op.batch_alter_table("symptom_dept_map") as batch:
            batch.create_foreign_key(
                "symptom_dept_map_tenant_id_fkey", "tenants", ["tenant_id"], ["id"]
            )
            batch.alter_column("tenant_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    op.drop_constraint(None, "symptom_dept_map", type_="foreignkey")
    op.drop_column("symptom_dept_map", "tenant_id")
    op.drop_constraint(None, "departments", type_="foreignkey")
    op.drop_column("departments", "tenant_id")
    op.drop_table("tenants")
