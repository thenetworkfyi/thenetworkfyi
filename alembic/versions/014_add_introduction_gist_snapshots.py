"""Store sanitized gist snapshots for post-consent match recaps.

Revision ID: 014
Revises: 013
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "introduction_consents",
        sa.Column("person_a_gist", sa.Text(), nullable=True),
    )
    op.add_column(
        "introduction_consents",
        sa.Column("person_b_gist", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("introduction_consents", "person_b_gist")
    op.drop_column("introduction_consents", "person_a_gist")
