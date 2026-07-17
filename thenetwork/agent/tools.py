"""pydantic-ai agent tools for The Network.

Security contracts (THE SEAL) are structurally enforced here:
- remember/search: cross-user memories return gist (PII-stripped) + opaque ids only
- reply_to_sender: sender identity resolved server-side, never model-selected
- send_outreach: opaque recipient_user_id, address resolved server-side
- Role separation: untrusted body arrives as user-role, never touches system prompt

Tool result policy: expected world-state and policy outcomes return a dict with
``status`` rather than raising to the model. ``error`` means correct arguments
once or escalate; ``limited``, ``deferred``, ``forbidden``, and ``suppressed``
are final for this run. Pydantic AI gets one retry only for argument validation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from limits import parse, strategies
from pydantic_ai import RunContext
from sqlalchemy import func
from sqlmodel import select

from thenetwork.agent.deps import AgentDeps
from thenetwork.audit import audit_event, audit_span, audit_span_completion
from thenetwork.db.models import (
    Event,
    EventRecommendation,
    EventSuppression,
    Memory,
    Person,
)
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.email.outbound import (
    FIRST_CONTACT_WELCOME_REPLY,
    _direct_reply_kwargs,
    notify_admins,
    reply_subject,
    send_reply,
)
from thenetwork.memory.sanitize import (
    sanitize_memory_high_fidelity,
    sanitize_text_high_fidelity,
)
from thenetwork.security.rate_limit import PostgresFixedWindowStorage
from thenetwork.introductions import propose_pair
from thenetwork.search.match import MemoryMatch, match_memories
from thenetwork.search.events import EventMatch, match_events

MAX_CONSOLIDATION_CANDIDATES = 3
# match_memories returns one row per ref, so a single multi-ref memory can
# occupy several rows; over-fetch before deduping by memory_id so a run of
# duplicate rows doesn't crowd out a genuinely distinct candidate.
_CONSOLIDATION_QUERY_LIMIT = MAX_CONSOLIDATION_CANDIDATES * 4
_dispatch_limiter: strategies.FixedWindowRateLimiter | None = None
_dispatch_storage: PostgresFixedWindowStorage | None = None
_registration_limiter: strategies.FixedWindowRateLimiter | None = None
_registration_storage: PostgresFixedWindowStorage | None = None

FIRST_EVENT_RECOMMENDATION_NOTICE = (
    "Would you like occasional event recommendations like this? Reply yes or no. "
    "A no stops only event recommendations."
)
EVENT_RECOMMENDATION_STOP_NOTICE = (
    'To stop event recommendations, reply "stop event recommendations."'
)
EVENT_RECOMMENDATION_SUBJECT = "An event you might care about"


class _SanitizationFailed(Exception):
    """A referenced memory could not be given its required sealed gist."""


def _get_session(ctx: RunContext[AgentDeps]):
    sf = ctx.deps.session_factory
    return sf() if sf is not None else get_session()


def _get_dispatch_limiter() -> tuple[strategies.FixedWindowRateLimiter, object]:
    global _dispatch_limiter, _dispatch_storage
    if _dispatch_limiter is None:
        _dispatch_storage = PostgresFixedWindowStorage()
        _dispatch_limiter = strategies.FixedWindowRateLimiter(_dispatch_storage)
    return _dispatch_limiter, _dispatch_storage


def _cap(value: int) -> int:
    return max(0, value)


def _limited(reason: str, limit: int) -> dict[str, Any]:
    return {"status": "limited", "reason": reason, "limit": limit}


def _tool_result(result: dict[str, Any]) -> dict[str, Any]:
    audit_fields: dict[str, object] = {
        "tool_outcome": result.get("status", "success"),
    }
    if "reason" in result:
        audit_fields["tool_reason"] = result["reason"]
    audit_span_completion(**audit_fields)
    return result


def _introduction_result(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a `propose_introduction` outcome for `_tool_result`.

    Every status but `proposed` reads like a pending action if you only see
    the tool name ("propose_introduction" ran, so *something* must have gone
    out). Attach an explicit note so a reply-writing model doesn't need to
    infer "no request was sent" from the status vocabulary alone.
    """
    if result.get("status") == "proposed":
        return _tool_result(result)
    return _tool_result(
        {
            **result,
            "note": (
                "no consent request was sent to either person for this call - "
                "do not tell the sender an introduction request went out or "
                "that opt-in requests will arrive"
            ),
        }
    )


def _trace_kwargs(trace_id: str | None) -> dict[str, str]:
    return {"trace_id": trace_id} if trace_id else {}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_event_expiry(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _event_projection(event: Event) -> dict[str, Any]:
    """Return only the sealed event fields allowed into model context."""
    return {
        "event_id": event.id,
        "gist": event.gist,
        "expires_at": event.expires_at,
        "cancelled": event.cancelled_at is not None,
    }


def _event_sanitization_source(text: str, recurrence: str | None) -> str:
    if not recurrence:
        return text
    return f"{text}\nRecurrence: {recurrence}"


def _authenticated_person_id(ctx: RunContext[AgentDeps]) -> str | None:
    if not ctx.deps.sender_authenticated:
        return None
    return ctx.deps.sender_user_id


def _check_daily_dispatch_cap(key: str, limit: int) -> bool:
    """Return whether `key` still has quota, without consuming any."""
    if limit <= 0:
        return False
    limiter, _ = _get_dispatch_limiter()
    # The check/consume split avoids charging failed SMTP sends. It is a
    # bounded race across workers: the atomic consume is still durable.
    try:
        return limiter.test(parse(f"{limit}/day"), key)
    except Exception:
        return False


def _consume_daily_dispatch_cap(key: str, limit: int) -> None:
    """Consume one unit of `key`'s quota. Call only after the send succeeds -
    a crashed/retried job must not have already burned the cap."""
    if limit <= 0:
        return
    limiter, _ = _get_dispatch_limiter()
    limiter.hit(parse(f"{limit}/day"), key)


def _get_registration_limiter() -> tuple[strategies.FixedWindowRateLimiter, object]:
    global _registration_limiter, _registration_storage
    if _registration_limiter is None:
        _registration_storage = PostgresFixedWindowStorage()
        _registration_limiter = strategies.FixedWindowRateLimiter(_registration_storage)
    return _registration_limiter, _registration_storage


def _hit_registration_quota(ctx: RunContext[AgentDeps]) -> bool:
    limit_per_day = ctx.deps.settings.registration_limit_per_day
    if limit_per_day <= 0:
        return True
    limiter, _ = _get_registration_limiter()
    try:
        return limiter.hit(parse(f"{limit_per_day}/day"), "registrations:global")
    except Exception:
        return False


def _person_memory_count(session, person_id: str) -> int:
    return session.exec(
        select(func.count())
        .select_from(Memory)
        .where(Memory.refs.contains([person_id]))
    ).one()


def _memory_ceiling_error(
    ctx: RunContext[AgentDeps],
    session,
    refs: list[str],
) -> dict[str, Any] | None:
    limit = ctx.deps.settings.person_memory_limit
    if limit <= 0:
        return None

    for person_id in sorted(set(refs)):
        if _person_memory_count(session, person_id) >= limit:
            return {
                "status": "error",
                "reason": "person_memory_limit_exceeded",
                "person_id": person_id,
                "limit": limit,
            }
    return None


async def _embed_memory_for_write(memory: Memory, session) -> None:
    if memory.refs:
        try:
            gist = await sanitize_memory_high_fidelity(memory, session)
        except Exception as exc:
            raise _SanitizationFailed from exc
        if memory.gist is None and isinstance(gist, str):
            memory.gist = gist
        if memory.gist is None:
            raise _SanitizationFailed
        memory.embedding = await embed_text(memory.gist)
        return

    memory.embedding = await embed_text(memory.text)


async def remember(
    ctx: RunContext[AgentDeps],
    text: str,
    refs: list[str],
) -> dict[str, Any]:
    """Persist a new memory and return its ID plus sealed consolidation hints.

    refs is a list of person ids this memory is about. 0 refs = general
    knowledge; 1 ref = attribute of one person; 2+ refs = graph edge.
    A gist (PII-stripped) is automatically produced for all non-empty refs
    so the memory is eligible for cross-user retrieval (SEAL requirement).
    Each consolidation candidate contains only a memory ID, gist, and
    similarity to the newly stored memory.
    """
    with audit_span("agent.tool", tool_name="remember", refs_count=len(refs)):
        max_chars = ctx.deps.settings.remember_text_max_chars
        if max_chars > 0 and len(text) > max_chars:
            return _tool_result(
                {
                    "status": "error",
                    "reason": "memory_text_too_long",
                    "limit": max_chars,
                }
            )

        memory = Memory(text=text, refs=refs)
        with _get_session(ctx) as session:
            ceiling_error = _memory_ceiling_error(ctx, session, refs)
            if ceiling_error is not None:
                return _tool_result(ceiling_error)

            session.add(memory)
            try:
                await _embed_memory_for_write(memory, session)
            except _SanitizationFailed:
                # A referenced memory must never become searchable without a
                # sanitized gist. Roll back the pending insert rather than
                # letting an agent-tool exception escape the run.
                session.rollback()
                audit_event(
                    "database.action",
                    action="insert",
                    record_type="memory",
                    refs_count=len(refs),
                    outcome="blocked",
                )
                return _tool_result(
                    {
                        "status": "error",
                        "reason": "sanitization_failed",
                    }
                )
            memory_id = memory.id
            query_embedding = list(memory.embedding or [])
            session.commit()
            matches: list[MemoryMatch] = []
            if query_embedding:
                matches = match_memories(
                    query_embedding,
                    session,
                    limit=_CONSOLIDATION_QUERY_LIMIT,
                    exclude_memory_id=memory_id,
                )
        audit_event(
            "database.action",
            action="insert",
            record_type="memory",
            refs_count=len(refs),
            outcome="success",
        )
        candidates = []
        seen_memory_ids: set[str] = set()
        for m in matches:
            if m.memory_id == memory_id or m.memory_id in seen_memory_ids:
                continue
            seen_memory_ids.add(m.memory_id)
            candidates.append(
                {
                    "memory_id": m.memory_id,
                    "gist": m.gist,
                    "similarity": round(m.similarity, 3),
                }
            )
            if len(candidates) == MAX_CONSOLIDATION_CANDIDATES:
                break
        return _tool_result(
            {
                "memory_id": memory_id,
                "consolidation_candidates": candidates,
            }
        )


async def forget(ctx: RunContext[AgentDeps], memory_id: str) -> dict[str, str]:
    """Delete a memory by ID.

    To consolidate duplicates or replace a stale/contradictory fact, forget
    the superseded memory ID and `remember` the corrected fact - never try to
    mutate a memory in place (edit = forget + remember).

    Strict sole-ref ownership: only a memory whose refs are exactly
    `[sender_user_id]` may be forgotten. Any other memory - unowned (0 refs),
    owned by someone else, or co-owned (2+ refs) - returns
    `{"status": "forbidden", "reason": "not_sender_memory"}` and is not
    deleted, regardless of how the request is phrased.
    """
    with audit_span("agent.tool", tool_name="forget"):
        with _get_session(ctx) as session:
            memory = session.get(Memory, memory_id)
            if memory is None:
                audit_event(
                    "database.action",
                    action="delete",
                    record_type="memory",
                    outcome="not_found",
                )
                return _tool_result({"status": "not_found"})
            sender_user_id = ctx.deps.sender_user_id
            refs = memory.refs or []
            if not sender_user_id or refs != [sender_user_id]:
                audit_event(
                    "database.action",
                    action="delete",
                    record_type="memory",
                    outcome="rejected_forbidden",
                )
                return _tool_result(
                    {
                        "status": "forbidden",
                        "reason": "not_sender_memory",
                    }
                )
            session.delete(memory)
            session.commit()
        audit_event(
            "database.action",
            action="delete",
            record_type="memory",
            outcome="success",
        )
        return _tool_result({"status": "deleted"})


async def search(
    ctx: RunContext[AgentDeps],
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Semantic search over person-referencing memories.

    Returns opaque person_id + PII-stripped gist only. Raw text, names, and
    email addresses of other users are NEVER returned (SEAL contract).
    """
    with audit_span(
        "agent.tool",
        tool_name="search",
        query_chars=len(query),
        top_k=top_k,
    ):
        max_chars = ctx.deps.settings.search_query_max_chars
        if max_chars > 0 and len(query) > max_chars:
            return _tool_result(
                {
                    "status": "error",
                    "reason": "query_too_long",
                    "limit": max_chars,
                }
            )

        query_vec = await embed_text(query)
        with _get_session(ctx) as session:
            matches: list[MemoryMatch] = match_memories(query_vec, session, limit=top_k)
        audit_event(
            "database.action",
            action="search",
            record_type="memory",
            result_count=len(matches),
            outcome="success",
        )
        results = []
        for m in matches:
            result = {
                "person_id": m.person_id,
                "gist": m.gist,
                "similarity": round(m.similarity, 3),
                "is_sender_owned": m.person_id == ctx.deps.sender_user_id,
            }
            if result["is_sender_owned"]:
                result["memory_id"] = m.memory_id
            results.append(result)
        audit_span_completion(tool_outcome="success")
        return results


async def create_event(
    ctx: RunContext[AgentDeps],
    text: str,
    expires_at: str,
    recurrence: str | None = None,
) -> dict[str, Any]:
    """Create an owner-controlled one-off event or recurring event series.

    Raw content remains in the owner-controlled event row. The returned and
    searchable form is always a sanitized gist with an embedding derived from
    that gist, never from the raw text.
    """
    with audit_span("agent.tool", tool_name="create_event"):
        owner_id = _authenticated_person_id(ctx)
        if owner_id is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        sanitization_source = _event_sanitization_source(text, recurrence)
        max_chars = ctx.deps.settings.remember_text_max_chars
        if max_chars > 0 and len(sanitization_source) > max_chars:
            return _tool_result(
                {"status": "error", "reason": "event_text_too_long", "limit": max_chars}
            )
        expiry = _parse_event_expiry(expires_at)
        if expiry is None:
            return _tool_result({"status": "error", "reason": "invalid_event_expiry"})
        if expiry <= datetime.now(timezone.utc):
            return _tool_result(
                {"status": "error", "reason": "event_expiry_not_future"}
            )
        try:
            gist = await sanitize_text_high_fidelity(sanitization_source)
            embedding = await embed_text(gist)
        except Exception:
            return _tool_result({"status": "error", "reason": "sanitization_failed"})

        event = Event(
            submitter_id=owner_id,
            text=text,
            gist=gist,
            embedding=embedding,
            recurrence=recurrence,
            expires_at=expiry,
        )
        with _get_session(ctx) as session:
            session.add(event)
            projection = _event_projection(event)
            session.commit()
        audit_event(
            "database.action",
            action="insert",
            record_type="event",
            outcome="success",
        )
        return _tool_result({"status": "created", **projection})


async def update_event(
    ctx: RunContext[AgentDeps],
    event_id: str,
    text: str,
    expires_at: str,
    recurrence: str | None = None,
) -> dict[str, Any]:
    """Replace an authenticated sender's event content without changing its id."""
    with audit_span("agent.tool", tool_name="update_event"):
        owner_id = _authenticated_person_id(ctx)
        if owner_id is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        sanitization_source = _event_sanitization_source(text, recurrence)
        max_chars = ctx.deps.settings.remember_text_max_chars
        if max_chars > 0 and len(sanitization_source) > max_chars:
            return _tool_result(
                {"status": "error", "reason": "event_text_too_long", "limit": max_chars}
            )
        expiry = _parse_event_expiry(expires_at)
        if expiry is None:
            return _tool_result({"status": "error", "reason": "invalid_event_expiry"})
        if expiry <= datetime.now(timezone.utc):
            return _tool_result(
                {"status": "error", "reason": "event_expiry_not_future"}
            )

        with _get_session(ctx) as session:
            event = session.get(Event, event_id)
            if event is None:
                return _tool_result({"status": "not_found"})
            if event.submitter_id != owner_id:
                return _tool_result(
                    {"status": "forbidden", "reason": "not_event_owner"}
                )
            if event.cancelled_at is not None:
                return _tool_result(
                    {"status": "forbidden", "reason": "event_cancelled"}
                )
            try:
                gist = await sanitize_text_high_fidelity(sanitization_source)
                embedding = await embed_text(gist)
            except Exception:
                session.rollback()
                return _tool_result(
                    {"status": "error", "reason": "sanitization_failed"}
                )
            event.text = text
            event.gist = gist
            event.embedding = embedding
            event.recurrence = recurrence
            event.expires_at = expiry
            event.version += 1
            event.updated_at = datetime.now(timezone.utc)
            session.add(event)
            projection = _event_projection(event)
            session.commit()
        audit_event(
            "database.action",
            action="update",
            record_type="event",
            outcome="success",
        )
        return _tool_result({"status": "updated", **projection})


async def cancel_event(ctx: RunContext[AgentDeps], event_id: str) -> dict[str, Any]:
    """Cancel an authenticated sender's event; other users cannot mutate it."""
    with audit_span("agent.tool", tool_name="cancel_event"):
        owner_id = _authenticated_person_id(ctx)
        if owner_id is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        with _get_session(ctx) as session:
            event = session.get(Event, event_id)
            if event is None:
                return _tool_result({"status": "not_found"})
            if event.submitter_id != owner_id:
                return _tool_result(
                    {"status": "forbidden", "reason": "not_event_owner"}
                )
            if event.cancelled_at is None:
                now = datetime.now(timezone.utc)
                event.cancelled_at = now
                event.updated_at = now
                session.add(event)
                projection = _event_projection(event)
                session.commit()
                status = "cancelled"
            else:
                status = "already_cancelled"
                projection = _event_projection(event)
        return _tool_result({"status": status, **projection})


async def search_events(
    ctx: RunContext[AgentDeps], query: str, top_k: int = 5
) -> list[dict[str, Any]] | dict[str, Any]:
    """Search active events through the gist-only SQL projection."""
    with audit_span(
        "agent.tool", tool_name="search_events", query_chars=len(query), top_k=top_k
    ):
        if _authenticated_person_id(ctx) is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        max_chars = ctx.deps.settings.search_query_max_chars
        if max_chars > 0 and len(query) > max_chars:
            return _tool_result(
                {"status": "error", "reason": "query_too_long", "limit": max_chars}
            )
        query_vec = await embed_text(query)
        with _get_session(ctx) as session:
            matches: list[EventMatch] = match_events(
                query_vec, session, limit=min(max(top_k, 0), 20)
            )
        audit_span_completion(tool_outcome="success")
        return [
            {
                "event_id": match.event_id,
                "gist": match.gist,
                "expires_at": match.expires_at,
                "similarity": round(match.similarity, 3),
            }
            for match in matches
        ]


async def stop_event_recommendations(
    ctx: RunContext[AgentDeps],
) -> dict[str, Any]:
    """Suppress only event FYIs for the authenticated sender."""
    with audit_span("agent.tool", tool_name="stop_event_recommendations"):
        person_id = _authenticated_person_id(ctx)
        if person_id is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        with _get_session(ctx) as session:
            suppression = session.get(EventSuppression, person_id)
            if suppression is None:
                session.add(EventSuppression(person_id=person_id))
                session.commit()
                status = "suppressed"
            else:
                status = "already_suppressed"
        audit_event(
            "database.action",
            action="insert",
            record_type="event_suppression",
            outcome="success" if status == "suppressed" else "exists",
        )
        return _tool_result({"status": status})


async def resume_event_recommendations(
    ctx: RunContext[AgentDeps],
) -> dict[str, Any]:
    """Resume only event FYIs for the authenticated sender."""
    with audit_span("agent.tool", tool_name="resume_event_recommendations"):
        person_id = _authenticated_person_id(ctx)
        if person_id is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        with _get_session(ctx) as session:
            suppression = session.get(EventSuppression, person_id)
            if suppression is None:
                status = "already_enabled"
            else:
                session.delete(suppression)
                session.commit()
                status = "resumed"
        audit_event(
            "database.action",
            action="delete",
            record_type="event_suppression",
            outcome="success" if status == "resumed" else "not_found",
        )
        return _tool_result({"status": status})


async def escalate(ctx: RunContext[AgentDeps], reason: str) -> dict[str, str]:
    """Flag this email for human review and notify admin.

    Use when intent is ambiguous, the request is outside your capabilities, or
    you have low confidence. A human will follow up with the sender directly.
    Do not use this for an ordinary first contact that can be registered and
    answered with reply_to_sender. The fixed welcome/how-to-join reply is only
    a fallback for authenticated senders who are still unknown when escalation
    is requested, so they learn how to use the address without giving the model
    control over that copy.
    """
    with audit_span("agent.tool", tool_name="escalate"):
        s = ctx.deps.settings
        sender = ctx.deps.sender_email

        if ctx.deps.sender_authenticated and ctx.deps.sender_user_id is None:
            send_reply(
                to_address=sender,
                subject=reply_subject(ctx.deps.inbound_subject, fallback="How to join"),
                body_text=FIRST_CONTACT_WELCOME_REPLY,
                include_footer=False,
                **_trace_kwargs(ctx.deps.trace_id),
                **_direct_reply_kwargs(
                    inbound_message_id=ctx.deps.inbound_message_id,
                    inbound_body_for_quote=ctx.deps.inbound_body_for_quote,
                    inbound_date=ctx.deps.inbound_date,
                    inbound_references=ctx.deps.inbound_references,
                ),
            )
            audit_event("agent.first_contact_welcome_sent")
            subject = f"[The Network] Manual reply needed: {sender}"
            body = (
                f"Email from {sender} was escalated for human review.\n\n"
                f"Reason: {reason}\n\n"
                f"Trace ID: {ctx.deps.trace_id or 'unavailable'}\n\n"
                f"Please reply to {sender} manually."
            )
            notify_admins(s, subject, body, trace_id=ctx.deps.trace_id)
            return _tool_result({"status": "welcomed_and_escalated"})

        refs = [ctx.deps.sender_user_id] if ctx.deps.sender_user_id else []

        text = f"[ESCALATED] {reason}"
        memory = Memory(text=text, refs=refs)
        with _get_session(ctx) as session:
            session.add(memory)
            await _embed_memory_for_write(memory, session)
            session.commit()
            memory_id = memory.id
        audit_event(
            "database.action",
            action="insert",
            record_type="memory",
            refs_count=len(refs),
            outcome="success",
        )
        subject = f"[The Network] Manual reply needed: {sender}"
        body = (
            f"Email from {sender} was escalated for human review.\n\n"
            f"Reason: {reason}\n\n"
            f"Please reply to {sender} manually."
        )
        notify_admins(s, subject, body, trace_id=ctx.deps.trace_id)

        return _tool_result({"status": "escalated", "memory_id": memory_id})


async def no_action(ctx: RunContext[AgentDeps], reason: str) -> str:
    """Declare that this email genuinely warrants no reply, outreach, or memory.

    Use for spam, automated mail, or content with no genuine human ask - not
    for a real person's question or request, even one outside what you do
    (answer that with `reply_to_sender` or use `escalate` if you cannot
    determine a safe response). Calling this tool is how "do nothing" is
    recorded; do not leave a run silent by simply not calling any tool, since
    that is indistinguishable from having forgotten to act. This is a no-op:
    it does not notify anyone and is not itself a form of human review - use
    `escalate` when you are unsure rather than calling this to end the run.
    """
    with audit_span("agent.tool", tool_name="no_action"):
        audit_span_completion(tool_outcome="no_action")
        return ""


async def register_person(
    ctx: RunContext[AgentDeps],
    name: str,
) -> dict[str, str]:
    """Create a Person record for a brand-new sender's first contact.

    Self-registration only: the server uses the sender's own authenticated
    From: address, and the sender must not already be a known Person. The model
    never supplies a raw address here - accepting one from message content
    would reopen the confused-deputy risk of model-selected recipients
    exists to prevent.

    Returns the new person_id. For a normal first contact, use that id in
    `remember` refs for what the sender shared, then reply with `reply_to_sender`.
    """
    with audit_span("agent.tool", tool_name="register_person"):
        if not ctx.deps.sender_authenticated:
            audit_event(
                "database.action",
                action="insert",
                record_type="person",
                outcome="rejected_unauthenticated",
            )
            return _tool_result(
                {
                    "status": "error",
                    "reason": "sender_not_authenticated",
                }
            )

        if ctx.deps.sender_user_id is not None:
            audit_event(
                "database.action",
                action="insert",
                record_type="person",
                outcome="rejected_already_registered",
            )
            return _tool_result(
                {
                    "status": "error",
                    "reason": "already_registered",
                    "person_id": ctx.deps.sender_user_id,
                }
            )

        with _get_session(ctx) as session:
            existing = session.exec(
                select(Person).where(Person.email == ctx.deps.sender_email)
            ).first()
            if existing:
                ctx.deps.sender_user_id = existing.id
                audit_event(
                    "database.action",
                    action="lookup",
                    record_type="person",
                    outcome="exists",
                )
                return _tool_result({"status": "exists", "person_id": existing.id})

            if not _hit_registration_quota(ctx):
                audit_event(
                    "database.action",
                    action="insert",
                    record_type="person",
                    outcome="rate_limited",
                )
                return _tool_result(
                    {
                        "status": "error",
                        "reason": "registration_quota_exceeded",
                        "limit": ctx.deps.settings.registration_limit_per_day,
                    }
                )

            person = Person(email=ctx.deps.sender_email, name=name)
            session.add(person)
            session.commit()
            session.refresh(person)
            person_id = person.id
            ctx.deps.sender_user_id = person_id

        audit_event(
            "database.action",
            action="insert",
            record_type="person",
            outcome="success",
        )
        return _tool_result({"status": "created", "person_id": person_id})


async def _send_email(
    ctx: RunContext[AgentDeps],
    recipient_user_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    *,
    is_sender_reply: bool,
    tool_name: str,
) -> dict[str, Any]:
    with audit_span(
        "agent.tool",
        tool_name=tool_name,
        recipient_id_present=bool(recipient_user_id),
        subject_chars=len(subject),
        body_chars=len(body_text),
        html_present=body_html is not None,
    ):
        s = ctx.deps.settings
        if ctx.deps.sender_user_id is None:
            audit_event(
                "database.action",
                action="lookup",
                record_type="person",
                outcome="rejected_sender_not_registered",
            )
            return _tool_result(
                {
                    "status": "error",
                    "reason": "sender_not_registered",
                }
            )

        max_sends_per_run = _cap(s.dispatch_max_sends_per_run)
        if ctx.deps.outbound_send_count >= max_sends_per_run:
            return _tool_result(_limited("max_sends_per_run", max_sends_per_run))

        with _get_session(ctx) as session:
            person = session.get(Person, recipient_user_id)
            to_address = person.email if person is not None else None

        audit_event(
            "database.action",
            action="lookup",
            record_type="person",
            outcome="found" if person is not None else "not_found",
        )
        if person is None or to_address is None:
            return _tool_result(
                {
                    "status": "error",
                    "reason": "recipient_not_found",
                }
            )

        recipient_daily_cap = _cap(s.dispatch_recipient_daily_cap)
        recipient_cap_key = f"dispatch:recipient:{recipient_user_id}"
        if not _check_daily_dispatch_cap(recipient_cap_key, recipient_daily_cap):
            return _tool_result(_limited("recipient_daily_cap", recipient_daily_cap))

        sender_reply_daily_cap = _cap(s.dispatch_sender_reply_daily_cap)
        sender_reply_cap_key = f"dispatch:sender-reply:{recipient_user_id}"
        if is_sender_reply and not _check_daily_dispatch_cap(
            sender_reply_cap_key, sender_reply_daily_cap
        ):
            return _tool_result(
                _limited("sender_reply_daily_cap", sender_reply_daily_cap)
            )

        thread_headers = {}
        if is_sender_reply and ctx.deps.inbound_message_id:
            thread_headers = _direct_reply_kwargs(
                inbound_message_id=ctx.deps.inbound_message_id,
                inbound_body_for_quote=ctx.deps.inbound_body_for_quote,
                inbound_date=ctx.deps.inbound_date,
                inbound_references=ctx.deps.inbound_references,
            )

        send_reply(
            to_address=to_address,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            **_trace_kwargs(ctx.deps.trace_id),
            **thread_headers,
        )

        # Only burn cap quota once the send has actually succeeded, so a
        # failed attempt (and its Procrastinate retry) isn't rate-limited
        # out of ever replying.
        _consume_daily_dispatch_cap(recipient_cap_key, recipient_daily_cap)
        if is_sender_reply:
            _consume_daily_dispatch_cap(sender_reply_cap_key, sender_reply_daily_cap)

        ctx.deps.outbound_send_count += 1
        ctx.deps.server_side_send_count += 1
        return _tool_result({"status": "sent"})


async def reply_to_sender(
    ctx: RunContext[AgentDeps],
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> dict[str, Any]:
    """Reply to this inbound email's registered sender.

    The caller cannot select a recipient. The server derives the recipient
    solely from the inbound sender, and only this capability receives inbound
    threading and quoted-message context.
    """
    if ctx.deps.sender_user_id is None:
        with audit_span("agent.tool", tool_name="reply_to_sender"):
            audit_event(
                "database.action",
                action="lookup",
                record_type="person",
                outcome="rejected_sender_not_registered",
            )
            return _tool_result(
                {
                    "status": "error",
                    "reason": "sender_not_registered",
                }
            )
    return await _send_email(
        ctx,
        recipient_user_id=ctx.deps.sender_user_id,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        is_sender_reply=True,
        tool_name="reply_to_sender",
    )


async def send_outreach(
    ctx: RunContext[AgentDeps],
    recipient_user_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> dict[str, Any]:
    """Send a new, unthreaded email to another user by opaque ID.

    This is deliberately separate from ``reply_to_sender``. It never receives
    inbound threading headers or quoted inbound content, and cannot be used to
    reply to the current sender.
    """
    if recipient_user_id == ctx.deps.sender_user_id:
        with audit_span("agent.tool", tool_name="send_outreach"):
            return _tool_result(
                {
                    "status": "error",
                    "reason": "use_reply_to_sender",
                }
            )
    return await _send_email(
        ctx,
        recipient_user_id=recipient_user_id,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        is_sender_reply=False,
        tool_name="send_outreach",
    )


async def send_event_recommendation(
    ctx: RunContext[AgentDeps],
    event_id: str,
) -> dict[str, Any]:
    """Send one event FYI to the bound proactive recipient.

    The recipient is always the authenticated person this run is acting for;
    the caller cannot select an address or another user id. The selected event
    is likewise bound by the server-authored trigger. Lifecycle, suppression,
    and duplicate gates are checked while the recommendation ledger row is
    locked. ``notified_at`` is written only after SMTP succeeds.
    """
    with audit_span("agent.tool", tool_name="send_event_recommendation"):
        recipient_id = _authenticated_person_id(ctx)
        if recipient_id is None:
            return _tool_result(
                {"status": "error", "reason": "sender_not_authenticated"}
            )
        if (
            not ctx.deps.is_proactive
            or not ctx.deps.proactive_event_id
            or ctx.deps.proactive_event_version is None
            or event_id != ctx.deps.proactive_event_id
        ):
            return _tool_result(
                {"status": "forbidden", "reason": "outside_event_trigger"}
            )

        now = datetime.now(timezone.utc)
        with _get_session(ctx) as session:
            event = session.get(Event, event_id)
            if event is None:
                return _tool_result(
                    {"status": "not_found", "reason": "event_not_found"}
                )
            if event.cancelled_at is not None:
                return _tool_result(
                    {"status": "suppressed", "reason": "event_cancelled"}
                )
            if _utc(event.expires_at) <= now:
                return _tool_result({"status": "suppressed", "reason": "event_expired"})
            if event.submitter_id == recipient_id:
                return _tool_result({"status": "suppressed", "reason": "self_event"})
            if session.get(EventSuppression, recipient_id) is not None:
                return _tool_result(
                    {"status": "suppressed", "reason": "event_recommendations_stopped"}
                )

            recommendation = session.exec(
                select(EventRecommendation)
                .where(
                    EventRecommendation.event_id == event_id,
                    EventRecommendation.person_id == recipient_id,
                )
                .with_for_update()
            ).first()
            if recommendation is None:
                return _tool_result(
                    {"status": "forbidden", "reason": "event_not_considered"}
                )
            if recommendation.notified_at is not None:
                return _tool_result(
                    {"status": "suppressed", "reason": "event_already_notified"}
                )
            if (
                event.version != ctx.deps.proactive_event_version
                or recommendation.event_version != ctx.deps.proactive_event_version
            ):
                return _tool_result(
                    {"status": "suppressed", "reason": "event_version_changed"}
                )

            prior_delivery_count = session.exec(
                select(func.count())
                .select_from(EventRecommendation)
                .where(
                    EventRecommendation.person_id == recipient_id,
                    EventRecommendation.notified_at.is_not(None),
                )
            ).one()
            notice = (
                FIRST_EVENT_RECOMMENDATION_NOTICE
                if prior_delivery_count == 0
                else EVENT_RECOMMENDATION_STOP_NOTICE
            )
            result = await _send_email(
                ctx,
                recipient_user_id=recipient_id,
                subject=EVENT_RECOMMENDATION_SUBJECT,
                body_text=(
                    f"An event that may be relevant:\n\n{event.gist}\n\n{notice}"
                ),
                is_sender_reply=False,
                tool_name="send_event_recommendation",
            )
            if result.get("status") != "sent":
                return result

            recommendation.notified_at = datetime.now(timezone.utc)
            session.add(recommendation)
            session.commit()
        audit_event(
            "database.action",
            action="update",
            record_type="event_recommendation",
            outcome="success",
        )
        return result


async def propose_introduction(
    ctx: RunContext[AgentDeps],
    other_person_id: str,
    sender_gist: str,
    other_gist: str,
) -> dict[str, Any]:
    """Propose a match without revealing either participant.

    The server derives one participant from the authenticated sender, stores
    the unordered pair, and sends fixed consent requests. The model supplies
    only sealed gists and the other person's opaque id.
    """
    with audit_span("agent.tool", tool_name="propose_introduction"):
        if not ctx.deps.sender_authenticated or ctx.deps.sender_user_id is None:
            return _introduction_result(
                {
                    "status": "error",
                    "reason": "sender_not_authenticated",
                }
            )
        if other_person_id == ctx.deps.sender_user_id:
            refusal: dict[str, Any] = {
                "status": "error",
                "reason": "self_introduction",
                "hint": (
                    "other_person_id was the id of the person you are acting "
                    "for; their side of the pair is derived server-side. "
                    "Retry once with the other person's opaque id."
                ),
            }
            if ctx.deps.is_proactive and ctx.deps.proactive_candidate_id:
                refusal["expected_other_person_id"] = ctx.deps.proactive_candidate_id
            return _introduction_result(refusal)
        if ctx.deps.is_proactive and (
            not ctx.deps.proactive_candidate_id
            or other_person_id != ctx.deps.proactive_candidate_id
        ):
            refusal = {
                "status": "error",
                "reason": "outside_proactive_pair",
            }
            if ctx.deps.proactive_candidate_id:
                refusal["hint"] = (
                    "this proactive run may only propose the surfaced "
                    "counterpart. Retry once with the expected id."
                )
                refusal["expected_other_person_id"] = ctx.deps.proactive_candidate_id
            return _introduction_result(refusal)
        proposal_limit = ctx.deps.settings.introduction_max_proposals_per_run
        if (
            proposal_limit > 0
            and ctx.deps.introduction_proposal_count >= proposal_limit
        ):
            return _introduction_result(
                {
                    "status": "deferred",
                    "reason": "run_proposal_cap",
                    "limit": proposal_limit,
                }
            )
        result = propose_pair(
            sender_person_id=ctx.deps.sender_user_id,
            other_person_id=other_person_id,
            sender_gist=sender_gist,
            other_gist=other_gist,
            session_factory=ctx.deps.session_factory or get_session,
            trace_id=ctx.deps.trace_id,
            max_outstanding_requests_per_person=(
                ctx.deps.settings.introduction_max_outstanding_requests_per_person
            ),
            max_requests_per_person_in_window=(
                ctx.deps.settings.introduction_max_requests_per_person_in_window
            ),
            request_window_seconds=(
                ctx.deps.settings.introduction_request_window_seconds
            ),
            decline_cooldown_days=ctx.deps.settings.consent_decline_cooldown_days,
        )
        if result.get("status") == "proposed":
            ctx.deps.server_side_send_count += 2
            ctx.deps.introduction_proposal_count += 1
        return _introduction_result(result)
