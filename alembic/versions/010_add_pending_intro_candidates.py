"""Queue of proactive introduction candidates awaiting a digest send.

Revision ID: 010
Revises: 009
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_intro_candidates",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("recipient_person_id", sa.String(), nullable=False),
        sa.Column("candidate_person_id", sa.String(), nullable=False),
        sa.Column("candidate_gist", sa.Text(), nullable=False),
        sa.Column("recipient_gist", sa.Text(), nullable=False),
        sa.Column("digest_token", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipient_person_id"], ["people.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_person_id"], ["people.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "recipient_person_id", "candidate_person_id", name="uq_pending_intro_pair"
        ),
    )
    op.create_index(
        "ix_pending_intro_candidates_recipient_person_id",
        "pending_intro_candidates",
        ["recipient_person_id"],
    )
    op.create_index(
        "ix_pending_intro_candidates_candidate_person_id",
        "pending_intro_candidates",
        ["candidate_person_id"],
    )
    op.create_index(
        "ix_pending_intro_candidates_digest_token",
        "pending_intro_candidates",
        ["digest_token"],
    )
    op.create_index(
        "ix_pending_intro_candidates_status",
        "pending_intro_candidates",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("pending_intro_candidates")
