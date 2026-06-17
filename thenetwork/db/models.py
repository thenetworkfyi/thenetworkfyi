from __future__ import annotations

import uuid as _uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
from sqlmodel import Field, Relationship, SQLModel


def _new_uuid() -> str:
    return str(_uuid.uuid4())


class Profile(SQLModel, table=True):
    __tablename__ = "profiles"

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    name: str
    email: str = Field(unique=True, index=True)
    bio: str = ""
    # stored as postgres text[] via SA type; SQLModel maps list[str] -> ARRAY(Text)
    skills: list[str] = Field(default_factory=list, sa_column_kwargs={"type_": "text[]"})
    intent_description: str = ""
    available_to_collaborate: bool = True

    # pgvector columns — 1536 dims to match text-embedding-3-small
    identity_vector: Optional[list[float]] = Field(
        default=None, sa_column=Column(Vector(1536))
    )
    intent_vector: Optional[list[float]] = Field(
        default=None, sa_column=Column(Vector(1536))
    )

    connections_a: list["NetworkConnection"] = Relationship(
        back_populates="user_a",
        sa_relationship_kwargs={"foreign_keys": "[NetworkConnection.user_id_a]", "cascade": "all, delete-orphan"},
    )
    connections_b: list["NetworkConnection"] = Relationship(
        back_populates="user_b",
        sa_relationship_kwargs={"foreign_keys": "[NetworkConnection.user_id_b]", "cascade": "all, delete-orphan"},
    )


class NetworkConnection(SQLModel, table=True):
    __tablename__ = "network_connections"

    # Directed edges — two rows per undirected relationship (A→B and B→A).
    # This makes the NetworkX graph build trivial: one query, no mirroring logic.
    user_id_a: str = Field(foreign_key="profiles.id", primary_key=True)
    user_id_b: str = Field(foreign_key="profiles.id", primary_key=True)
    connection_strength: float = Field(default=1.0)

    user_a: Optional[Profile] = Relationship(
        back_populates="connections_a",
        sa_relationship_kwargs={"foreign_keys": "[NetworkConnection.user_id_a]"},
    )
    user_b: Optional[Profile] = Relationship(
        back_populates="connections_b",
        sa_relationship_kwargs={"foreign_keys": "[NetworkConnection.user_id_b]"},
    )
