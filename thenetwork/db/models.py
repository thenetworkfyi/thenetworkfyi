from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text, UniqueConstraint
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
    gist: Optional[str] = Field(default=None, sa_column=Column(Text(), nullable=True))
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
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


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


class PrimaryIntakeState(SQLModel, table=True):
    """Singleton durable control state for the primary IMAP intake."""

    __tablename__ = "primary_intake_state"

    key: str = Field(default="primary", primary_key=True)
    paused: bool = Field(default=False, nullable=False)
    pause_reason: Optional[str] = Field(
        default=None, sa_column=Column(Text(), nullable=True)
    )
    paused_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class IntroductionConsent(SQLModel, table=True):
    """Server-owned pairwise consent state for anonymous relay introductions."""

    __tablename__ = "introduction_consents"
    __table_args__ = (
        UniqueConstraint("person_a_id", "person_b_id", name="uq_introduction_pair"),
    )

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    person_a_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    person_b_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    reply_token: str = Field(default_factory=_new_uuid, unique=True, index=True)
    person_a_consented: bool = Field(default=False, nullable=False)
    person_b_consented: bool = Field(default=False, nullable=False)
    person_a_gist: Optional[str] = Field(
        default=None, sa_column=Column(Text(), nullable=True)
    )
    person_b_gist: Optional[str] = Field(
        default=None, sa_column=Column(Text(), nullable=True)
    )
    status: str = Field(default="proposed", nullable=False, index=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    declined_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class ProactiveSurface(SQLModel, table=True):
    """Opaque pair record used to rotate proactive outreach candidates."""

    __tablename__ = "proactive_surfaces"
    __table_args__ = (
        UniqueConstraint(
            "person_a_id", "person_b_id", name="uq_proactive_surface_pair"
        ),
    )

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    person_a_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    person_b_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    surfaced_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


class Event(SQLModel, table=True):
    """A stable, owner-controlled event or recurring event series.

    Event meaning stays in freeform content. Only the identity and lifecycle
    state that server code must enforce are structured here. A recurring
    series is one row with a freeform ``recurrence`` description, so it is
    recommended at most once per person rather than once per occurrence.
    """

    __tablename__ = "events"

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    submitter_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    text: str = Field(sa_column=Column(Text(), nullable=False))
    gist: str = Field(sa_column=Column(Text(), nullable=False))
    embedding: Optional[list[float]] = Field(
        default=None, sa_column=Column(Vector(1536))
    )
    recurrence: Optional[str] = Field(
        default=None, sa_column=Column(Text(), nullable=True)
    )
    version: int = Field(
        default=1,
        sa_column=Column(Integer(), nullable=False, server_default="1"),
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    cancelled_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class EventRecommendation(SQLModel, table=True):
    """One durable consideration/delivery ledger row per event and person."""

    __tablename__ = "event_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "person_id", name="uq_event_recommendation_event_person"
        ),
    )

    id: str = Field(default_factory=_new_uuid, primary_key=True)
    event_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    person_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    event_version: int = Field(
        default=1,
        sa_column=Column(Integer(), nullable=False, server_default="1"),
    )
    considered_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    notified_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class EventSuppression(SQLModel, table=True):
    """Person-level opt-out from event FYIs, independent of people matching."""

    __tablename__ = "event_suppressions"

    person_id: str = Field(
        sa_column=Column(
            Text(),
            ForeignKey("people.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    suppressed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
