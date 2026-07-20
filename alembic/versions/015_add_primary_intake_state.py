"""Add durable primary email intake control state.

Revision ID: 015
Revises: 014
Create Date: 2026-07-20
"""

from typing import Sequence, Union
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "primary_intake_state",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    intake_state = sa.table(
        "primary_intake_state",
        sa.column("key", sa.String()),
        sa.column("paused", sa.Boolean()),
        sa.column("pause_reason", sa.Text()),
        sa.column("paused_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        intake_state,
        [
            {
                "key": "primary",
                "paused": False,
                "pause_reason": None,
                "paused_at": None,
                "updated_at": datetime.now(timezone.utc),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("primary_intake_state")
