from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, SQLModel


def _new_uuid() -> str:
    return str(_uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Person(SQLModel, table=True):
    __tablename__ = "people"

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    email: str = Field(unique=True, index=True)
    name: str


class Memory(SQLModel, table=True):
    __tablename__ = "memories"

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    text: str = Field(sa_column=Column(Text(), nullable=False))
    embedding: Optional[list[float]] = Field(
        default=None, sa_column=Column(Vector(1536))
    )
    refs: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text()), nullable=False, server_default="{}"),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
