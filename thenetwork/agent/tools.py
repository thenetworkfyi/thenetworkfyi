"""pydantic-ai agent tools for The Network.

Security contracts (THE SEAL) are structurally enforced here:
- remember/search: cross-user memories return gist (PII-stripped) + opaque ids only
- dispatch_email: opaque recipient_user_id, address resolved server-side
- Role separation: untrusted body arrives as user-role, never touches system prompt
"""
from __future__ import annotations

from typing import Any

from limits import parse, storage, strategies
from pydantic_ai import RunContext
from sqlmodel import select

from thenetwork.agent.deps import AgentDeps
from thenetwork.audit import audit_event, audit_span
from thenetwork.db.models import Memory, Person
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.email.outbound import (
    FIRST_CONTACT_WELCOME_REPLY,
    _direct_reply_kwargs,
    notify_admins,
    reply_subject,
    send_reply,
)
from thenetwork.memory.sanitize import sanitize_memory_high_fidelity
from thenetwork.search.match import MemoryMatch, match_memories

MAX_CONSOLIDATION_CANDIDATES = 3
# match_memories returns one row per ref, so a single multi-ref memory can
# occupy several rows; over-fetch before deduping by memory_id so a run of
# duplicate rows doesn't crowd out a genuinely distinct candidate.
_CONSOLIDATION_QUERY_LIMIT = MAX_CONSOLIDATION_CANDIDATES * 4
_dispatch_limiter: strategies.MovingWindowRateLimiter | None = None
_dispatch_storage: storage.Storage | None = None
_registration_limiter: strategies.MovingWindowRateLimiter | None = None
_registration_storage: storage.Storage | None = None


def _get_session(ctx: RunContext[AgentDeps]):
    sf = ctx.deps.session_factory
    return sf() if sf is not None else get_session()


def _get_dispatch_limiter() -> tuple[strategies.MovingWindowRateLimiter, object]:
    global _dispatch_limiter, _dispatch_storage
    if _dispatch_limiter is None:
        _dispatch_storage = storage.MemoryStorage()
        _dispatch_limiter = strategies.MovingWindowRateLimiter(_dispatch_storage)
    return _dispatch_limiter, _dispatch_storage


def _cap(value: int) -> int:
    return max(0, value)


def _limited(reason: str, limit: int) -> dict[str, Any]:
    return {"status": "limited", "reason": reason, "limit": limit}


def _hit_daily_dispatch_cap(key: str, limit: int) -> bool:
    if limit <= 0:
        return False
    limiter, _ = _get_dispatch_limiter()
    return limiter.hit(parse(f"{limit}/day"), key)


def _get_registration_limiter() -> tuple[strategies.MovingWindowRateLimiter, object]:
    global _registration_limiter, _registration_storage
    if _registration_limiter is None:
        _registration_storage = storage.MemoryStorage()
        _registration_limiter = strategies.MovingWindowRateLimiter(_registration_storage)
    return _registration_limiter, _registration_storage


def _hit_registration_quota(ctx: RunContext[AgentDeps]) -> bool:
    limit_per_day = ctx.deps.settings.registration_limit_per_day
    if limit_per_day <= 0:
        return True
    limiter, _ = _get_registration_limiter()
    return limiter.hit(parse(f"{limit_per_day}/day"), "registrations:global")


def _person_memory_count(session, person_id: str) -> int:
    result = session.exec(
        select(Memory).where(Memory.refs.contains([person_id]))
    )
    return len(result.all())


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
        gist = await sanitize_memory_high_fidelity(memory, session)
        if memory.gist is None and isinstance(gist, str):
            memory.gist = gist
        if memory.gist is None:
            raise RuntimeError(
                f"Memory {memory.id} has refs but no gist after sanitization"
            )
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
    """
    with audit_span("agent.tool", tool_name="remember", refs_count=len(refs)):
        max_chars = ctx.deps.settings.remember_text_max_chars
        if max_chars > 0 and len(text) > max_chars:
            return {
                "status": "error",
                "reason": "memory_text_too_long",
                "limit": max_chars,
            }

        memory = Memory(text=text, refs=refs)
        with _get_session(ctx) as session:
            ceiling_error = _memory_ceiling_error(ctx, session, refs)
            if ceiling_error is not None:
                return ceiling_error

            session.add(memory)
            await _embed_memory_for_write(memory, session)
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
                    "score": round(m.similarity, 3),
                }
            )
            if len(candidates) == MAX_CONSOLIDATION_CANDIDATES:
                break
        return {"memory_id": memory_id, "consolidation_candidates": candidates}


async def forget(ctx: RunContext[AgentDeps], memory_id: str) -> dict[str, str]:
    """Delete a memory by ID.

    To consolidate duplicates or replace a stale/contradictory fact, forget
    the superseded memory ID and `remember` the corrected fact - never try to
    mutate a memory in place (edit = forget + remember).
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
                return {"status": "not_found"}
            sender_user_id = ctx.deps.sender_user_id
            refs = memory.refs or []
            if not sender_user_id or refs != [sender_user_id]:
                audit_event(
                    "database.action",
                    action="delete",
                    record_type="memory",
                    outcome="rejected_forbidden",
                )
                return {"status": "forbidden", "reason": "not_sender_memory"}
            session.delete(memory)
            session.commit()
        audit_event(
            "database.action",
            action="delete",
            record_type="memory",
            outcome="success",
        )
        return {"status": "deleted"}


async def search(
    ctx: RunContext[AgentDeps],
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
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
            raise ValueError("search query exceeds configured length cap")

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
            }
            if m.person_id == ctx.deps.sender_user_id:
                result["memory_id"] = m.memory_id
            results.append(result)
        return results


async def escalate(ctx: RunContext[AgentDeps], reason: str) -> dict[str, str]:
    """Flag this email for human review and notify admin.

    Use when intent is ambiguous, the request is outside your capabilities, or
    you have low confidence. A human will follow up with the sender directly.
    For authenticated first contact, send the fixed welcome/how-to-join reply
    instead of escalating; the sender learns how to use the address without
    giving the model control over the copy.
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
                **_direct_reply_kwargs(
                    ctx.deps.inbound_message_id,
                    ctx.deps.inbound_body_for_quote,
                    ctx.deps.inbound_date,
                ),
            )
            audit_event("agent.first_contact_welcome_sent")
            return {"status": "welcomed"}

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
        notify_admins(s, subject, body)

        return {"status": "escalated", "memory_id": memory_id}


async def register_person(
    ctx: RunContext[AgentDeps],
    email: str,
    name: str,
) -> dict[str, str]:
    """Create a Person record for a brand-new sender's first contact.

    Self-registration only: `email` must match the sender's own authenticated
    From: address, and the sender must not already be a known Person. This
    cannot be used to register anyone else - accepting an arbitrary raw
    address out of message content (e.g. to onboard a stranger mentioned in
    an introduction) would reopen the confused-deputy risk dispatch_email's
    opaque-id design exists to prevent, so that stays out of scope here.

    Returns the new person_id - use it for `refs` on subsequent `remember`
    calls and as the target of `dispatch_email` to reply to this sender.
    """
    with audit_span("agent.tool", tool_name="register_person"):
        if not ctx.deps.sender_authenticated:
            audit_event(
                "database.action",
                action="insert",
                record_type="person",
                outcome="rejected_unauthenticated",
            )
            return {"status": "error", "reason": "sender_not_authenticated"}

        if ctx.deps.sender_user_id is not None:
            return {
                "status": "error",
                "reason": "already_registered",
                "person_id": ctx.deps.sender_user_id,
            }

        if email.strip().lower() != ctx.deps.sender_email.strip().lower():
            return {"status": "error", "reason": "email_mismatch"}

        with _get_session(ctx) as session:
            existing = session.exec(
                select(Person).where(Person.email == ctx.deps.sender_email)
            ).first()
            if existing:
                return {"status": "exists", "person_id": existing.id}

            if not _hit_registration_quota(ctx):
                audit_event(
                    "database.action",
                    action="insert",
                    record_type="person",
                    outcome="rate_limited",
                )
                return {
                    "status": "error",
                    "reason": "registration_quota_exceeded",
                    "limit": ctx.deps.settings.registration_limit_per_day,
                }

            person = Person(email=ctx.deps.sender_email, name=name)
            session.add(person)
            session.commit()
            session.refresh(person)
            person_id = person.id

        audit_event(
            "database.action",
            action="insert",
            record_type="person",
            outcome="success",
        )
        return {"status": "created", "person_id": person_id}


async def dispatch_email(
    ctx: RunContext[AgentDeps],
    recipient_user_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> dict[str, Any]:
    """Send an email to a user by opaque ID.

    The LLM supplies only the opaque internal ID; this function resolves the
    real address server-side. The LLM never handles or sees raw email addresses
    (capability-style confused-deputy fix).
    """
    with audit_span(
        "agent.tool",
        tool_name="dispatch_email",
        recipient_id_present=bool(recipient_user_id),
        subject_chars=len(subject),
        body_chars=len(body_text),
        html_present=body_html is not None,
    ):
        s = ctx.deps.settings
        max_sends_per_run = _cap(s.dispatch_max_sends_per_run)
        if ctx.deps.dispatch_email_sent_count >= max_sends_per_run:
            return _limited("max_sends_per_run", max_sends_per_run)

        with _get_session(ctx) as session:
            person = session.get(Person, recipient_user_id)

        audit_event(
            "database.action",
            action="lookup",
            record_type="person",
            outcome="found" if person is not None else "not_found",
        )
        if person is None:
            return {"status": "error", "reason": "recipient_not_found"}

        recipient_daily_cap = _cap(s.dispatch_recipient_daily_cap)
        if not _hit_daily_dispatch_cap(
            f"dispatch:recipient:{recipient_user_id}",
            recipient_daily_cap,
        ):
            return _limited("recipient_daily_cap", recipient_daily_cap)

        sender_reply_daily_cap = _cap(s.dispatch_sender_reply_daily_cap)
        if recipient_user_id == ctx.deps.sender_user_id and not _hit_daily_dispatch_cap(
            f"dispatch:sender-reply:{recipient_user_id}",
            sender_reply_daily_cap,
        ):
            return _limited("sender_reply_daily_cap", sender_reply_daily_cap)

        thread_headers = {}
        if recipient_user_id == ctx.deps.sender_user_id and ctx.deps.inbound_message_id:
            thread_headers = _direct_reply_kwargs(
                ctx.deps.inbound_message_id,
                ctx.deps.inbound_body_for_quote,
                ctx.deps.inbound_date,
            )

        send_reply(
            to_address=person.email,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            **thread_headers,
        )
        ctx.deps.dispatch_email_sent_count += 1
        return {"status": "sent"}
