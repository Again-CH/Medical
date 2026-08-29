"""add exam_steps

Revision ID: a1b2c3d4e5f6
Revises: 21d137bd21a7
Create Date: 2026-08-28 23:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "21d137bd21a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exam_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_username", sa.String(length=64), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("step_name", sa.String(length=64), nullable=False),
        sa.Column("location", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_exam_steps_patient_username"),
        "exam_steps",
        ["patient_username"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_exam_steps_patient_username"), table_name="exam_steps")
    op.drop_table("exam_steps")
