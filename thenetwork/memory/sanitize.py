from __future__ import annotations

import re

from sqlmodel import Session

from thenetwork.db.models import Memory

_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
_PHONE_RE = re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')


def _strip_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text


def sanitize_memory(memory: Memory, session: Session) -> str:
    """Produce and persist a gist for a person-referencing memory.

    Runs a deterministic PII strip (emails, phone numbers) and writes the
    result back as memory.gist. Call for any memory with refs before it is
    eligible for cross-user search (SEAL requirement).
    """
    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )
    gist = _strip_pii(memory.text)
    memory.gist = gist
    session.add(memory)
    session.flush()
    return gist


async def sanitize_memory_llm(memory: Memory, session: Session) -> str:
    """LLM-based sanitization: broader PII removal than the deterministic strip.

    Fixed system prompt, no tools, no external influence. Slower than
    sanitize_memory(); use when higher-fidelity gists are needed.
    """
    from pydantic_ai import Agent
    from thenetwork.settings import get_settings

    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )

    s = get_settings()
    _sanitizer: Agent[None, str] = Agent(
        model=s.agent_model,
        system_prompt=(
            "You are a PII sanitizer. You will receive a memory about a person. "
            "Return a version with all personally-identifying information removed: "
            "replace names with [name], email addresses with [email], phone numbers "
            "with [phone], and specific street addresses with [address]. "
            "Keep factual content (skills, interests, context). "
            "Return only the sanitized text, nothing else."
        ),
        output_type=str,
    )
    result = await _sanitizer.run(memory.text)
    gist = result.output
    memory.gist = gist
    session.add(memory)
    session.flush()
    return gist


async def sanitize_memory_high_fidelity(memory: Memory, session: Session) -> str:
    """Produce and persist a SEAL-safe gist, preferring the LLM sanitizer.

    The LLM path removes broader PII such as names and street addresses. If
    that path fails for any reason, fall back to the deterministic sanitizer so
    person-referencing memories still get a gist before cross-user search.
    """
    try:
        return await sanitize_memory_llm(memory, session)
    except Exception:
        return sanitize_memory(memory, session)
