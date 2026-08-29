"""auth hardening: token_version/lockout columns + refresh_tokens table.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-29

新增：
- users / doctors 增加 token_version(全局吊销)、failed_attempts(防爆破计数)、
  locked_until(锁定到期) 三列；
- refresh_tokens 表（仅存刷新令牌哈希，支持按条吊销 + 访问令牌全局吊销）。
可回滚：drop 新表 + drop 新列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    # users
    op.add_column(
        "users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "users", sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(), nullable=True))
    # doctors
    op.add_column(
        "doctors", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "doctors", sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("doctors", sa.Column("locked_until", sa.DateTime(), nullable=True))
    # refresh_tokens
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_refresh_tokens_username", "refresh_tokens", ["username"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)


def downgrade():
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_username", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_column("doctors", "locked_until")
    op.drop_column("doctors", "failed_attempts")
    op.drop_column("doctors", "token_version")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_attempts")
    op.drop_column("users", "token_version")
