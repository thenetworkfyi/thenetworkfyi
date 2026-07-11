"""Add admin_nonces table for admin channel replay protection

Revision ID: 003
Revises: 002
Create Date: 2026-07-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_nonces",
        sa.Column("nonce", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_nonces_created_at", "admin_nonces", ["created_at"])


def downgrade() -> None:
    op.drop_table("admin_nonces")
