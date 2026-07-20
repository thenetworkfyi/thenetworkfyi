"""Add the hourly primary intake abuse judge cursor.

Revision ID: 017
Revises: 016
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "primary_intake_judge_state",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_mailbox_uid", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verdict", sa.Text(), nullable=True),
        sa.Column("last_reason", sa.Text(), nullable=True),
    )
    judge_state = sa.table(
        "primary_intake_judge_state",
        sa.column("key", sa.String()),
    )
    op.bulk_insert(judge_state, [{"key": "primary"}])


def downgrade() -> None:
    op.drop_table("primary_intake_judge_state")
