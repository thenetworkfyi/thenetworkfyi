"""Record recently surfaced proactive pairs.

Revision ID: 009
Revises: 008
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proactive_surfaces",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("person_a_id", sa.String(), nullable=False),
        sa.Column("person_b_id", sa.String(), nullable=False),
        sa.Column("surfaced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_a_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_b_id"], ["people.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("person_a_id", "person_b_id", name="uq_proactive_surface_pair"),
    )
    op.create_index(
        "ix_proactive_surfaces_surfaced_at", "proactive_surfaces", ["surfaced_at"]
    )


def downgrade() -> None:
    op.drop_table("proactive_surfaces")
