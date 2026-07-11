"""Add pairwise introduction consent state.

Revision ID: 007
Revises: 006
Create Date: 2026-07-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "introduction_consents",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("person_a_id", sa.String(), nullable=False),
        sa.Column("person_b_id", sa.String(), nullable=False),
        sa.Column("reply_token", sa.String(), nullable=False),
        sa.Column("person_a_consented", sa.Boolean(), nullable=False),
        sa.Column("person_b_consented", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_a_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_b_id"], ["people.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("person_a_id", "person_b_id", name="uq_introduction_pair"),
        sa.UniqueConstraint("reply_token", name="uq_introduction_reply_token"),
    )
    op.create_index(
        "ix_introduction_consents_person_a_id",
        "introduction_consents",
        ["person_a_id"],
    )
    op.create_index(
        "ix_introduction_consents_person_b_id",
        "introduction_consents",
        ["person_b_id"],
    )
    op.create_index(
        "ix_introduction_consents_reply_token",
        "introduction_consents",
        ["reply_token"],
    )
    op.create_index(
        "ix_introduction_consents_status",
        "introduction_consents",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("introduction_consents")
