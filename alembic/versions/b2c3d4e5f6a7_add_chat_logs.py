"""add chat_logs

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-29 00:10:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("patient_id", sa.String(length=64), nullable=True),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("intent", sa.String(length=32), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("tool_used", sa.String(length=32), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("fallback", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_logs_trace_id"), "chat_logs", ["trace_id"], unique=False)
    op.create_index(op.f("ix_chat_logs_patient_id"), "chat_logs", ["patient_id"], unique=False)
    op.create_index(op.f("ix_chat_logs_thread_id"), "chat_logs", ["thread_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_logs_thread_id"), table_name="chat_logs")
    op.drop_index(op.f("ix_chat_logs_patient_id"), table_name="chat_logs")
    op.drop_index(op.f("ix_chat_logs_trace_id"), table_name="chat_logs")
    op.drop_table("chat_logs")
