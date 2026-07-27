"""Best-effort memory records for successful user-facing email delivery.

The mail paths call this only after SMTP has returned successfully. Recording is
therefore deliberately fail-soft: a sanitizer, embedding, limit, or database
failure must never turn a completed delivery into a retry and duplicate email.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import select

from thenetwork.audit import audit_event
from thenetwork.db.models import Memory, Person
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.memory.sanitize import sanitize_memory
from thenetwork.settings import Settings, get_settings

SENT_EMAIL_SUMMARY_MAX_CHARS = 500

CONSENT_REQUEST_SUMMARY = (
    "an introduction consent request about an anonymous potential match"
)
CONSENT_CLARIFICATION_SUMMARY = (
    "a clarification asking for a yes, no, or revoke response to an "
    "introduction consent request"
)
CONSENT_ACKNOWLEDGMENT_SUMMARY = (
    "confirmation that introduction consent was recorded while the other "
    "participant's response is pending"
)
CONSENT_DECLINED_SUMMARY = "confirmation that an introduction was declined"
CONSENT_ALREADY_DECLINED_SUMMARY = (
    "notice that the introduction had already been declined"
)
INTRODUCTION_SUMMARY = (
    "a mutually approved introduction connecting the two participants"
)
FIRST_CONTACT_WELCOME_SUMMARY = "instructions for joining and using The Network"


@dataclass(frozen=True)
class SentEmailMemory:
    """One successful delivery's opaque recipient and content-free summary."""

    recipient_person_id: str
    summary: str


def event_recommendation_summary(event_gist: str) -> str:
    """Return the deterministic summary for a sealed event recommendation."""
    return f"an event recommendation about {event_gist}"


def _normalized_summary(summary: str) -> str:
    return " ".join(summary.split())[:SENT_EMAIL_SUMMARY_MAX_CHARS].rstrip()


async def record_sent_email_memory(
    delivery: SentEmailMemory,
    *,
    session_factory: Callable = get_session,
    settings: Settings | None = None,
) -> bool:
    """Persist one ordinary sealed memory without ever failing the caller.

    Only ``delivery.summary`` is stored. Email subjects, bodies, addresses, and
    headers are intentionally absent from this boundary.
    """
    summary = _normalized_summary(delivery.summary)
    if not delivery.recipient_person_id or not summary:
        audit_event(
            "database.action",
            action="insert",
            record_type="sent_email_memory",
            refs_count=1 if delivery.recipient_person_id else 0,
            outcome="blocked",
            reason="invalid_summary",
        )
        return False

    active_settings = settings or get_settings()
    text = f"Sent email: {summary}"
    if (
        active_settings.remember_text_max_chars > 0
        and len(text) > active_settings.remember_text_max_chars
    ):
        audit_event(
            "database.action",
            action="insert",
            record_type="sent_email_memory",
            refs_count=1,
            outcome="blocked",
            reason="memory_text_too_long",
        )
        return False

    session = None
    try:
        with session_factory() as session:
            if session.get(Person, delivery.recipient_person_id) is None:
                audit_event(
                    "database.action",
                    action="insert",
                    record_type="sent_email_memory",
                    refs_count=1,
                    outcome="blocked",
                    reason="recipient_not_found",
                )
                return False

            limit = active_settings.person_memory_limit
            if limit > 0:
                count = session.exec(
                    select(func.count())
                    .select_from(Memory)
                    .where(Memory.refs.contains([delivery.recipient_person_id]))
                ).one()
                if count >= limit:
                    audit_event(
                        "database.action",
                        action="insert",
                        record_type="sent_email_memory",
                        refs_count=1,
                        outcome="blocked",
                        reason="person_memory_limit_exceeded",
                    )
                    return False

            memory = Memory(text=text, refs=[delivery.recipient_person_id])
            session.add(memory)
            gist = sanitize_memory(memory, session)
            if memory.gist is None and isinstance(gist, str):
                memory.gist = gist
            if not memory.gist:
                raise RuntimeError("sent-email memory sanitization produced no gist")
            memory.embedding = await embed_text(memory.gist)
            session.commit()
    except Exception as exc:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        audit_event(
            "database.action",
            action="insert",
            record_type="sent_email_memory",
            refs_count=1,
            outcome="error",
            error_type=type(exc).__name__,
        )
        return False

    audit_event(
        "database.action",
        action="insert",
        record_type="sent_email_memory",
        refs_count=1,
        outcome="success",
    )
    return True


async def record_sent_email_memories(
    deliveries: Iterable[SentEmailMemory],
    *,
    session_factory: Callable = get_session,
    settings: Settings | None = None,
) -> None:
    """Record one memory per delivered recipient, preserving delivery success."""
    for delivery in deliveries:
        try:
            await record_sent_email_memory(
                delivery,
                session_factory=session_factory,
                settings=settings,
            )
        except Exception as exc:
            audit_event(
                "database.action",
                action="insert",
                record_type="sent_email_memory",
                refs_count=1,
                outcome="error",
                error_type=type(exc).__name__,
            )
