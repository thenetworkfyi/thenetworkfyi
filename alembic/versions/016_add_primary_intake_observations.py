"""Add PII-safe primary intake observations.

Revision ID: 016
Revises: 015
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "primary_intake_observations",
        sa.Column("mailbox_uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_authenticated", sa.Boolean(), nullable=False),
        sa.Column("sender_known", sa.Boolean(), nullable=False),
        sa.Column("sender_fingerprint", sa.String(), nullable=False),
        sa.Column("domain_fingerprint", sa.String(), nullable=False),
        sa.Column("body_fingerprint", sa.String(), nullable=False),
        sa.UniqueConstraint("trace_id", name="uq_primary_intake_observation_trace"),
    )
    op.create_index(
        "ix_primary_intake_observations_trace_id",
        "primary_intake_observations",
        ["trace_id"],
    )
    op.create_index(
        "ix_primary_intake_observations_observed_at",
        "primary_intake_observations",
        ["observed_at"],
    )
    op.create_index(
        "ix_primary_intake_observations_sender_known",
        "primary_intake_observations",
        ["sender_known"],
    )
    for column in (
        "sender_fingerprint",
        "domain_fingerprint",
        "body_fingerprint",
    ):
        op.create_index(
            f"ix_primary_intake_observations_{column}",
            "primary_intake_observations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("primary_intake_observations")
