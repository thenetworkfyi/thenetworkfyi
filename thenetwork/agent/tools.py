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

import hashlib
import inspect
import json
from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from limits import parse, strategies
from pydantic_ai import RunContext
from pydantic_ai.messages import RetryPromptPart
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
    EVENT_RECOMMENDATION_SUBJECT,
    _direct_reply_kwargs,
    notify_admins,
    reply_subject,
    send_event_fyi,
    send_reply,
)
from thenetwork.email.render import (
    EventRecommendationNotice,
    FirstContactWelcomeEmailContext,
    FixedEmailTemplate,
)
from thenetwork.memory.sanitize import (
    sanitize_memory_high_fidelity,
    sanitize_text_high_fidelity,
)
from thenetwork.memory.sent_email import (
    CONSENT_REQUEST_SUMMARY,
    SentEmailMemory,
    event_recommendation_summary,
    record_sent_email_memories,
    record_sent_email_memory,
)
from thenetwork.security.rate_limit import (
    PostgresFixedWindowStorage,
    normalize_rate_limit_identity,
)
from thenetwork.introductions import propose_pair
from thenetwork.search.match import (
    MAX_CANDIDATE_CONTEXTS,
    MAX_EVIDENCE_GISTS_PER_PERSON,
    MemoryMatch,
    build_candidate_contexts,
    load_person_evidence,
    match_memories,
)
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
_PROACTIVE_BOUND_MUTATIONS = frozenset(
    {"propose_introduction", "send_event_recommendation"}
)

FIRST_EVENT_RECOMMENDATION_NOTICE = EventRecommendationNotice.FIRST.value
EVENT_RECOMMENDATION_STOP_NOTICE = EventRecommendationNotice.STOP.value


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


def _retry_generation(ctx: RunContext[AgentDeps]) -> int:
    """Count server-created retry prompts visible before this tool call."""
    return sum(
        isinstance(part, RetryPromptPart)
        for message in getattr(ctx, "messages", ())
        for part in getattr(message, "parts", ())
    )


def _tool_argument_fingerprint(
    tool_name: str,
    signature: inspect.Signature,
    ctx: RunContext[AgentDeps],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Hash validated arguments without retaining or logging their raw values."""
    bound = signature.bind(ctx, *args, **kwargs)
    bound.apply_defaults()
    arguments = {
        name: value for name, value in bound.arguments.items() if name != "ctx"
    }
    payload = json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=_replay_json_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replay_json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported replay argument: {type(value).__name__}")


def _idempotent_mutation(function):
    """Replay completed mutating calls after a model retry prompt.

    Occurrence numbers preserve intentionally repeated identical calls in the
    original generation. A later retry generation maps the same ordered calls
    back to their completed results while differently shaped calls still run.
    The lock also prevents concurrent duplicate calls from racing the cache.
    """
    signature = inspect.signature(function)
    tool_name = function.__name__

    @wraps(function)
    async def wrapped(ctx: RunContext[AgentDeps], *args, **kwargs):
        if ctx.deps.is_proactive and tool_name not in _PROACTIVE_BOUND_MUTATIONS:
            with audit_span("agent.tool", tool_name=tool_name):
                return _tool_result(
                    {
                        "status": "forbidden",
                        "reason": "proactive_read_only",
                    }
                )
        fingerprint = _tool_argument_fingerprint(
            tool_name, signature, ctx, args, kwargs
        )
        generation = _retry_generation(ctx)
        async with ctx.deps.mutating_tool_lock:
            count_key = (generation, fingerprint)
            occurrence = ctx.deps.mutating_tool_generation_counts.get(count_key, 0)
            ctx.deps.mutating_tool_generation_counts[count_key] = occurrence + 1
            replay_key = (fingerprint, occurrence)
            if generation > 0 and replay_key in ctx.deps.mutating_tool_results:
                replay = {
                    "status": "replayed",
                    "tool_name": tool_name,
                    "original_result": ctx.deps.mutating_tool_results[replay_key],
                }
                with audit_span("agent.tool", tool_name=tool_name):
                    audit_event(
                        "agent.tool.replayed",
                        tool_name=tool_name,
                        outcome="replayed",
                    )
                    return _tool_result(replay)

            result = await function(ctx, *args, **kwargs)
            ctx.deps.mutating_tool_results[replay_key] = result
            return result

    return wrapped


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


def _unknown_sender_declines_participation(ctx: RunContext[AgentDeps]) -> bool:
    """Recognize an explicit refusal before an identity has been created.

    This is a narrow safety gate, not an intent classifier. Its only effect is
    to prevent registration and escalation side effects for an authenticated
    unknown sender who plainly says not to retain data or participate.
    """
    if ctx.deps.sender_user_id is not None:
        return False
    text = f"{ctx.deps.inbound_subject}\n{ctx.deps.inbound_body}".casefold()
    text = text.replace("’", "'")
    return any(
        phrase in text
        for phrase in (
            "opt out",
            "opting out",
            "do not retain",
            "don't retain",
            "do not store",
            "don't store",
            "do not register",
            "don't register",
            "do not want to participate",
            "don't want to participate",
        )
    )


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


@_idempotent_mutation
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


@_idempotent_mutation
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
    """Discover people and return bounded sealed evidence grouped by person.

    Similarity ranks candidate discovery; it is not a fit score. Cross-user
    evidence contains only PII-stripped gists plus an opaque person id. For
    sender-owned evidence only, each item also carries its memory id so the
    sender can consolidate or forget their own facts. Raw text, names, and
    email addresses are NEVER returned (SEAL contract).
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
        if top_k < 1 or top_k > MAX_CANDIDATE_CONTEXTS:
            return _tool_result(
                {
                    "status": "error",
                    "reason": "top_k_out_of_range",
                    "limit": MAX_CANDIDATE_CONTEXTS,
                }
            )

        query_vec = await embed_text(query)
        with _get_session(ctx) as session:
            matches: list[MemoryMatch] = match_memories(
                query_vec,
                session,
                limit=top_k * MAX_EVIDENCE_GISTS_PER_PERSON,
            )
            candidate_ids = list(dict.fromkeys(match.person_id for match in matches))[
                :top_k
            ]
            supporting = load_person_evidence(session, candidate_ids)
        contexts = build_candidate_contexts(
            matches,
            supporting,
            max_candidates=top_k,
        )
        audit_event(
            "database.action",
            action="search",
            record_type="memory",
            result_count=len(contexts),
            outcome="success",
        )
        results = []
        for candidate in contexts:
            is_sender_owned = candidate.person_id == ctx.deps.sender_user_id
            evidence = []
            for item in candidate.evidence:
                projected = {"gist": item.gist}
                if is_sender_owned:
                    projected["memory_id"] = item.memory_id
                evidence.append(projected)
            result = {
                "person_id": candidate.person_id,
                "evidence": evidence,
                "similarity": round(candidate.similarity, 3),
                "is_sender_owned": is_sender_owned,
            }
            results.append(result)
        audit_span_completion(tool_outcome="success")
        return results


@_idempotent_mutation
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


@_idempotent_mutation
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


@_idempotent_mutation
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


@_idempotent_mutation
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


@_idempotent_mutation
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


@_idempotent_mutation
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
            if _unknown_sender_declines_participation(ctx):
                return _tool_result(
                    {
                        "status": "no_action",
                        "reason": "sender_declined_participation",
                    }
                )
            send_reply(
                to_address=sender,
                subject=reply_subject(ctx.deps.inbound_subject, fallback="How to join"),
                fixed_template=FixedEmailTemplate.FIRST_CONTACT_WELCOME,
                fixed_context=FirstContactWelcomeEmailContext(),
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
            ctx.deps.terminal_action_taken = True
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
        ctx.deps.terminal_action_taken = True

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


@_idempotent_mutation
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

        if _unknown_sender_declines_participation(ctx):
            return _tool_result(
                {
                    "status": "error",
                    "reason": "sender_declined_participation",
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
    recipient_user_id: str | None,
    subject: str,
    body_text: str,
    sent_email_summary: str,
    *,
    is_sender_reply: bool,
    tool_name: str,
) -> dict[str, Any]:
    def deliver(to_address: str) -> None:
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
            **_trace_kwargs(ctx.deps.trace_id),
            **thread_headers,
        )

    return await _dispatch_email(
        ctx,
        recipient_user_id=recipient_user_id,
        is_sender_reply=is_sender_reply,
        tool_name=tool_name,
        subject_chars=len(subject),
        body_chars=len(body_text),
        sent_email_summary=sent_email_summary,
        deliver=deliver,
    )


async def _send_event_fyi(
    ctx: RunContext[AgentDeps],
    *,
    recipient_user_id: str,
    event_gist: str,
    notice: EventRecommendationNotice,
) -> dict[str, Any]:
    """Dispatch one fixed event template after the ordinary capability gates."""
    return await _dispatch_email(
        ctx,
        recipient_user_id=recipient_user_id,
        is_sender_reply=False,
        tool_name="send_event_recommendation",
        subject_chars=len(EVENT_RECOMMENDATION_SUBJECT),
        body_chars=len(event_gist) + len(notice.value),
        sent_email_summary=event_recommendation_summary(event_gist),
        deliver=lambda to_address: send_event_fyi(
            to_address=to_address,
            event_gist=event_gist,
            notice=notice,
            **_trace_kwargs(ctx.deps.trace_id),
        ),
    )


async def _dispatch_email(
    ctx: RunContext[AgentDeps],
    *,
    recipient_user_id: str | None,
    is_sender_reply: bool,
    tool_name: str,
    subject_chars: int,
    body_chars: int,
    sent_email_summary: str,
    deliver: Callable[[str], None],
) -> dict[str, Any]:
    with audit_span(
        "agent.tool",
        tool_name=tool_name,
        recipient_id_present=bool(recipient_user_id),
        subject_chars=subject_chars,
        body_chars=body_chars,
    ):
        s = ctx.deps.settings
        direct_unknown_sender_reply = (
            is_sender_reply and ctx.deps.sender_user_id is None
        )
        if ctx.deps.sender_user_id is None and not direct_unknown_sender_reply:
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
        if direct_unknown_sender_reply and (
            not ctx.deps.sender_authenticated or not ctx.deps.sender_email
        ):
            return _tool_result(
                {
                    "status": "error",
                    "reason": "sender_not_authenticated",
                }
            )

        max_sends_per_run = _cap(s.dispatch_max_sends_per_run)
        if ctx.deps.outbound_send_count >= max_sends_per_run:
            return _tool_result(_limited("max_sends_per_run", max_sends_per_run))

        if direct_unknown_sender_reply:
            to_address = ctx.deps.sender_email
            normalized_sender = normalize_rate_limit_identity(to_address)
            cap_identity = hashlib.sha256(normalized_sender.encode("utf-8")).hexdigest()
        else:
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
            cap_identity = recipient_user_id

        recipient_daily_cap = _cap(s.dispatch_recipient_daily_cap)
        recipient_cap_key = f"dispatch:recipient:{cap_identity}"
        if not _check_daily_dispatch_cap(recipient_cap_key, recipient_daily_cap):
            return _tool_result(_limited("recipient_daily_cap", recipient_daily_cap))

        sender_reply_daily_cap = _cap(s.dispatch_sender_reply_daily_cap)
        sender_reply_cap_key = f"dispatch:sender-reply:{cap_identity}"
        if is_sender_reply and not _check_daily_dispatch_cap(
            sender_reply_cap_key, sender_reply_daily_cap
        ):
            return _tool_result(
                _limited("sender_reply_daily_cap", sender_reply_daily_cap)
            )

        deliver(to_address)

        # Only burn cap quota once the send has actually succeeded, so a
        # failed attempt (and its Procrastinate retry) isn't rate-limited
        # out of ever replying.
        _consume_daily_dispatch_cap(recipient_cap_key, recipient_daily_cap)
        if is_sender_reply:
            _consume_daily_dispatch_cap(sender_reply_cap_key, sender_reply_daily_cap)

        ctx.deps.outbound_send_count += 1
        ctx.deps.server_side_send_count += 1
        if recipient_user_id is not None:
            try:
                await record_sent_email_memory(
                    SentEmailMemory(
                        recipient_person_id=recipient_user_id,
                        summary=sent_email_summary,
                    ),
                    session_factory=ctx.deps.session_factory or get_session,
                    settings=s,
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
        return _tool_result({"status": "sent"})


@_idempotent_mutation
async def reply_to_sender(
    ctx: RunContext[AgentDeps],
    subject: str,
    body_text: str,
    sent_email_summary: str = "a response to the recipient's message",
) -> dict[str, Any]:
    """Reply to this inbound email's authenticated sender.

    The caller cannot select a recipient. The server derives the recipient
    solely from the inbound sender, and only this capability receives inbound
    threading and quoted-message context. ``sent_email_summary`` is a concise
    description of the email's purpose for the recipient's private memory; it
    must not repeat the subject, body, address, or headers.
    """
    return await _send_email(
        ctx,
        recipient_user_id=ctx.deps.sender_user_id,
        subject=subject,
        body_text=body_text,
        sent_email_summary=sent_email_summary,
        is_sender_reply=True,
        tool_name="reply_to_sender",
    )


@_idempotent_mutation
async def send_outreach(
    ctx: RunContext[AgentDeps],
    recipient_user_id: str,
    subject: str,
    body_text: str,
    sent_email_summary: str = "a new message relevant to the recipient",
) -> dict[str, Any]:
    """Send a new, unthreaded email to another user by opaque ID.

    This is deliberately separate from ``reply_to_sender``. It never receives
    inbound threading headers or quoted inbound content, and cannot be used to
    reply to the current sender. ``sent_email_summary`` is a concise description
    of the email's purpose for the recipient's private memory; it must not repeat
    the subject, body, address, or headers.
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
        sent_email_summary=sent_email_summary,
        is_sender_reply=False,
        tool_name="send_outreach",
    )


@_idempotent_mutation
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
                EventRecommendationNotice.FIRST
                if prior_delivery_count == 0
                else EventRecommendationNotice.STOP
            )
            result = await _send_event_fyi(
                ctx,
                recipient_user_id=recipient_id,
                event_gist=event.gist,
                notice=notice,
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


@_idempotent_mutation
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
            await record_sent_email_memories(
                (
                    SentEmailMemory(
                        recipient_person_id=ctx.deps.sender_user_id,
                        summary=CONSENT_REQUEST_SUMMARY,
                    ),
                    SentEmailMemory(
                        recipient_person_id=other_person_id,
                        summary=CONSENT_REQUEST_SUMMARY,
                    ),
                ),
                session_factory=ctx.deps.session_factory or get_session,
                settings=ctx.deps.settings,
            )
            ctx.deps.server_side_send_count += 2
            ctx.deps.introduction_proposal_count += 1
        return _introduction_result(result)
