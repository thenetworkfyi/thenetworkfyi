"""Add first-class event persistence and event-only recommendation state.

Revision ID: 012
Revises: 011
Create Date: 2026-07-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector


revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("submitter_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("gist", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("recurrence", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["submitter_id"], ["people.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_events_submitter_id", "events", ["submitter_id"])
    op.create_index("ix_events_expires_at", "events", ["expires_at"])

    op.create_table(
        "event_recommendations",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("considered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "event_id", "person_id", name="uq_event_recommendation_event_person"
        ),
    )
    op.create_index(
        "ix_event_recommendations_event_id", "event_recommendations", ["event_id"]
    )
    op.create_index(
        "ix_event_recommendations_person_id", "event_recommendations", ["person_id"]
    )

    op.create_table(
        "event_suppressions",
        sa.Column("person_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("event_suppressions")
    op.drop_table("event_recommendations")
    op.drop_table("events")
