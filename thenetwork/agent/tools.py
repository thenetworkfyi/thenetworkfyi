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

from typing import Any

from limits import parse, strategies
from pydantic_ai import RunContext
from sqlmodel import select

from thenetwork.agent.deps import AgentDeps
from thenetwork.audit import audit_event, audit_span, audit_span_completion
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
from thenetwork.security.rate_limit import PostgresFixedWindowStorage
from thenetwork.introductions import propose_pair
from thenetwork.search.match import MemoryMatch, match_memories

MAX_CONSOLIDATION_CANDIDATES = 3
# match_memories returns one row per ref, so a single multi-ref memory can
# occupy several rows; over-fetch before deduping by memory_id so a run of
# duplicate rows doesn't crowd out a genuinely distinct candidate.
_CONSOLIDATION_QUERY_LIMIT = MAX_CONSOLIDATION_CANDIDATES * 4
_dispatch_limiter: strategies.FixedWindowRateLimiter | None = None
_dispatch_storage: PostgresFixedWindowStorage | None = None
_registration_limiter: strategies.FixedWindowRateLimiter | None = None
_registration_storage: PostgresFixedWindowStorage | None = None


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


def _trace_kwargs(trace_id: str | None) -> dict[str, str]:
    return {"trace_id": trace_id} if trace_id else {}


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
    """
    with audit_span("agent.tool", tool_name="remember", refs_count=len(refs)):
        max_chars = ctx.deps.settings.remember_text_max_chars
        if max_chars > 0 and len(text) > max_chars:
            return _tool_result({
                "status": "error",
                "reason": "memory_text_too_long",
                "limit": max_chars,
            })

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
                return _tool_result({
                    "status": "error",
                    "reason": "sanitization_failed",
                })
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
        return _tool_result({
            "memory_id": memory_id,
            "consolidation_candidates": candidates,
        })


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
                return _tool_result({
                    "status": "forbidden",
                    "reason": "not_sender_memory",
                })
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
            return _tool_result({
                "status": "error",
                "reason": "query_too_long",
                "limit": max_chars,
            })

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
            return _tool_result({
                "status": "error",
                "reason": "sender_not_authenticated",
            })

        if ctx.deps.sender_user_id is not None:
            audit_event(
                "database.action",
                action="insert",
                record_type="person",
                outcome="rejected_already_registered",
            )
            return _tool_result({
                "status": "error",
                "reason": "already_registered",
                "person_id": ctx.deps.sender_user_id,
            })

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
                return _tool_result({
                    "status": "error",
                    "reason": "registration_quota_exceeded",
                    "limit": ctx.deps.settings.registration_limit_per_day,
                })

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
            return _tool_result({
                "status": "error",
                "reason": "sender_not_registered",
            })

        max_sends_per_run = _cap(s.dispatch_max_sends_per_run)
        if ctx.deps.dispatch_email_sent_count >= max_sends_per_run:
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
            return _tool_result({
                "status": "error",
                "reason": "recipient_not_found",
            })

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

        ctx.deps.dispatch_email_sent_count += 1
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
            return _tool_result({
                "status": "error",
                "reason": "sender_not_registered",
            })
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
            return _tool_result({
                "status": "error",
                "reason": "use_reply_to_sender",
            })
    return await _send_email(
        ctx,
        recipient_user_id=recipient_user_id,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        is_sender_reply=False,
        tool_name="send_outreach",
    )


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
            return _tool_result({
                "status": "error",
                "reason": "sender_not_authenticated",
            })
        proposal_limit = ctx.deps.settings.introduction_max_proposals_per_run
        if (
            proposal_limit > 0
            and ctx.deps.introduction_proposal_count >= proposal_limit
        ):
            return _tool_result({
                "status": "deferred",
                "reason": "run_proposal_cap",
                "limit": proposal_limit,
            })
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
        return _tool_result(result)
