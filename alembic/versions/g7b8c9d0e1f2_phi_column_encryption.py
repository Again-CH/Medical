"""PHI 列静态加密：users.phone / full_name 改为 TEXT

将直接标识符列（手机号、真实姓名）从定长 VARCHAR 改为 TEXT，以容纳
``EncryptedText`` 加密后的密文（密文长度 > 原定长，Postgres 下定长会拒绝超长写入）。
其余 PHI 列（chat_logs / approvals / exam_steps / 患者私有库）本就是 TEXT，
``EncryptedText`` 底层同为 TEXT，不产生 schema 差异，无需迁移。

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "g7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.alter_column(
            "users",
            "phone",
            existing_type=sa.String(32),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="phone::text",
        )
        op.alter_column(
            "users",
            "full_name",
            existing_type=sa.String(128),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="full_name::text",
        )
    elif dialect == "sqlite":
        # SQLite 长度无约束，但为保持 schema 一致仍显式改为 TEXT（batch 模式）
        with op.batch_alter_table("users") as batch:
            batch.alter_column("phone", type_=sa.Text(), existing_type=sa.String(32))
            batch.alter_column("full_name", type_=sa.Text(), existing_type=sa.String(128))


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.alter_column(
            "users",
            "phone",
            existing_type=sa.Text(),
            type_=sa.String(32),
            existing_nullable=True,
            postgresql_using="phone::text",
        )
        op.alter_column(
            "users",
            "full_name",
            existing_type=sa.Text(),
            type_=sa.String(128),
            existing_nullable=True,
            postgresql_using="full_name::text",
        )
    elif dialect == "sqlite":
        with op.batch_alter_table("users") as batch:
            batch.alter_column("full_name", type_=sa.String(128), existing_type=sa.Text())
            batch.alter_column("phone", type_=sa.String(32), existing_type=sa.Text())
