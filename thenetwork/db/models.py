from __future__ import annotations

import uuid as _uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, SQLModel


def _new_uuid() -> str:
    return str(_uuid.uuid4())


class Profile(SQLModel, table=True):
    __tablename__ = "profiles"

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    name: str
    email: str = Field(unique=True, index=True)
    bio: str = ""
    skills: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text()), nullable=False, server_default="{}"),
    )
    intent_description: str = ""
    available_to_collaborate: bool = True

    identity_vector: Optional[list[float]] = Field(
        default=None, sa_column=Column(Vector(1536))
    )
    intent_vector: Optional[list[float]] = Field(
        default=None, sa_column=Column(Vector(1536))
    )


class NetworkConnection(SQLModel, table=True):
    __tablename__ = "network_connections"

    # Directed edges — two rows per undirected relationship (A→B and B→A).
    # This makes the NetworkX graph build trivial: one query, no mirroring logic.
    user_id_a: str = Field(foreign_key="profiles.id", primary_key=True)
    user_id_b: str = Field(foreign_key="profiles.id", primary_key=True)
    connection_strength: float = Field(default=1.0)
