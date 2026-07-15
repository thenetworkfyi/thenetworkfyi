"""Drop the obsolete proactive introduction digest queue.

Revision ID: 011
Revises: 010
Create Date: 2026-07-15
"""

from alembic import op


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("pending_intro_candidates")


def downgrade() -> None:
    raise NotImplementedError("The removed digest queue is intentionally not restored.")
