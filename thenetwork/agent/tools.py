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
    vec = await embed_text(text)
    memory = Memory(text=text, refs=refs, embedding=vec)
    with _get_session(ctx) as session:
        session.add(memory)
        if refs:
            sanitize_memory(memory, session)
        session.commit()
    return {"memory_id": memory.id}


async def forget(ctx: RunContext[AgentDeps], memory_id: str) -> dict[str, str]:
    """Delete a memory by ID (own memories only; no cross-user deletion)."""
    with _get_session(ctx) as session:
        memory = session.get(Memory, memory_id)
        if memory is None:
            return {"status": "not_found"}
        session.delete(memory)
        session.commit()
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
    query_vec = await embed_text(query)
    with _get_session(ctx) as session:
        matches: list[MemoryMatch] = match_memories(query_vec, session, limit=top_k)
    return [
        {
            "person_id": m.person_id,
            "gist": m.gist,
            "similarity": round(m.similarity, 3),
        }
        for m in matches
    ]


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
    with _get_session(ctx) as session:
        person = session.get(Person, recipient_user_id)

    if person is None:
        return {"status": "error", "reason": "recipient_not_found"}

    send_reply(
        to_address=person.email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )
    return {"status": "sent"}
