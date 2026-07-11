from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint
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
    gist: Optional[str] = Field(
        default=None, sa_column=Column(Text(), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AdminNonce(SQLModel, table=True):
    """Replay guard for the admin channel's PGP/MIME-signed requests.

    Rows are pruned by admin/auth.py whenever a request is checked; there is
    no separate cleanup job because admin traffic is low-volume by design.
    """
    __tablename__ = "admin_nonces"

    nonce: str = Field(primary_key=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class RateLimit(SQLModel, table=True):
    """Durable counters for the inbound email rate limiter."""

    __tablename__ = "rate_limits"

    key: str = Field(primary_key=True)
    count: int = Field(nullable=False)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class ProcessedMessage(SQLModel, table=True):
    """Idempotency guard for inbound intake.

    The IMAP \\Seen flag is cheap dedup only (see worker/producer.py) - it can
    be cleared by a mail client, sync bug, or manual recovery step well after
    a message was already fully handled. This is the durable record that a
    given Message-ID already got a `process_email` job, so a later \\Seen
    reset can't cause the agent to re-run, re-reply, or re-dispatch an
    introduction for the same physical email. See email/dedup.py.
    """

    __tablename__ = "processed_messages"

    message_id: str = Field(primary_key=True)
    processed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class BannedEmail(SQLModel, table=True):
    """Emails that are banned/blocked from using the system."""

    __tablename__ = "banned_emails"

    email: str = Field(primary_key=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class IntroductionConsent(SQLModel, table=True):
    """Server-owned pairwise consent state for identity-revealing introductions."""

    __tablename__ = "introduction_consents"
    __table_args__ = (
        UniqueConstraint("person_a_id", "person_b_id", name="uq_introduction_pair"),
    )

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    person_a_id: str = Field(
        sa_column=Column(
            Text(), ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    person_b_id: str = Field(
        sa_column=Column(
            Text(), ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    reply_token: str = Field(default_factory=_new_uuid, unique=True, index=True)
    person_a_consented: bool = Field(default=False, nullable=False)
    person_b_consented: bool = Field(default=False, nullable=False)
    status: str = Field(default="proposed", nullable=False, index=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
