"""Add the timestamp used for temporary consent declines.

Revision ID: 008
Revises: 007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("introduction_consents", sa.Column("declined_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("introduction_consents", "declined_at")
