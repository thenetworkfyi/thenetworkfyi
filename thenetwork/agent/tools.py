"""pydantic-ai agent tools for The Network.

Security contracts (THE SEAL) are structurally enforced here:
- remember/search: cross-user memories return gist (PII-stripped) + opaque ids only
- dispatch_email: opaque recipient_user_id, address resolved server-side
- Role separation: untrusted body arrives as user-role, never touches system prompt
"""
from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext

from thenetwork.agent.deps import AgentDeps
from thenetwork.audit import audit_event, audit_span
from thenetwork.db.models import Memory, Person
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.email.outbound import send_reply
from thenetwork.memory.sanitize import sanitize_memory
from thenetwork.search.match import MemoryMatch, match_memories


def _get_session(ctx: RunContext[AgentDeps]):
    sf = ctx.deps.session_factory
    return sf() if sf is not None else get_session()


async def remember(
    ctx: RunContext[AgentDeps],
    text: str,
    refs: list[str],
) -> dict[str, str]:
    """Persist a new memory and return its ID.

    refs is a list of person ids this memory is about. 0 refs = general
    knowledge; 1 ref = attribute of one person; 2+ refs = graph edge.
    A gist (PII-stripped) is automatically produced for all non-empty refs
    so the memory is eligible for cross-user retrieval (SEAL requirement).
    """
    with audit_span("agent.tool", tool_name="remember", refs_count=len(refs)):
        vec = await embed_text(text)
        memory = Memory(text=text, refs=refs, embedding=vec)
        with _get_session(ctx) as session:
            session.add(memory)
            if refs:
                sanitize_memory(memory, session)
            session.commit()
        audit_event(
            "database.action",
            action="insert",
            record_type="memory",
            refs_count=len(refs),
            outcome="success",
        )
        return {"memory_id": memory.id}


async def forget(ctx: RunContext[AgentDeps], memory_id: str) -> dict[str, str]:
    """Delete a memory by ID (own memories only; no cross-user deletion)."""
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
        return [
            {
                "person_id": m.person_id,
                "gist": m.gist,
                "similarity": round(m.similarity, 3),
            }
            for m in matches
        ]


async def escalate(ctx: RunContext[AgentDeps], reason: str) -> dict[str, str]:
    """Flag this email for human review and notify admin. No auto-reply will be sent.

    Use when intent is ambiguous, the request is outside your capabilities, or
    you have low confidence. A human will follow up with the sender directly.
    """
    with audit_span("agent.tool", tool_name="escalate"):
        s = ctx.deps.settings
        sender = ctx.deps.sender_email
        refs = [ctx.deps.sender_user_id] if ctx.deps.sender_user_id else []

        text = f"[ESCALATED] {reason}"
        vec = await embed_text(text)
        memory = Memory(text=text, refs=refs, embedding=vec)
        with _get_session(ctx) as session:
            session.add(memory)
            if refs:
                sanitize_memory(memory, session)
            session.commit()
        audit_event(
            "database.action",
            action="insert",
            record_type="memory",
            refs_count=len(refs),
            outcome="success",
        )
        if s.admin_emails:
            subject = f"[The Network] Manual reply needed: {sender}"
            body = (
                f"Email from {sender} was escalated for human review.\n\n"
                f"Reason: {reason}\n\n"
                f"Please reply to {sender} manually."
            )
            for admin_email in s.admin_emails:
                send_reply(to_address=admin_email, subject=subject, body_text=body)

        return {"status": "escalated", "memory_id": memory.id}


async def dispatch_email(
    ctx: RunContext[AgentDeps],
    recipient_user_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> dict[str, str]:
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

        send_reply(
            to_address=person.email,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
        return {"status": "sent"}
