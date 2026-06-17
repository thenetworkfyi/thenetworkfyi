"""Initial schema with pgvector and pg_trgm extensions

Revision ID: 001
Revises:
Create Date: 2026-06-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "profiles",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("bio", sa.String(), nullable=False, server_default=""),
        sa.Column("skills", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("intent_description", sa.String(), nullable=False, server_default=""),
        sa.Column("available_to_collaborate", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("identity_vector", Vector(1536), nullable=True),
        sa.Column("intent_vector", Vector(1536), nullable=True),
    )
    op.create_index("ix_profiles_email", "profiles", ["email"], unique=True)

    op.create_table(
        "network_connections",
        sa.Column("user_id_a", sa.String(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("user_id_b", sa.String(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("connection_strength", sa.Float(), nullable=False, server_default="1.0"),
    )

    # HNSW indexes for ANN search (cosine distance)
    op.execute(
        "CREATE INDEX ix_profiles_identity_vector ON profiles "
        "USING hnsw (identity_vector vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_profiles_intent_vector ON profiles "
        "USING hnsw (intent_vector vector_cosine_ops)"
    )

    # GIN index on skills array for fast containment / overlap queries
    op.execute(
        "CREATE INDEX ix_profiles_skills ON profiles USING gin (skills)"
    )


def downgrade() -> None:
    op.drop_table("network_connections")
    op.drop_table("profiles")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
