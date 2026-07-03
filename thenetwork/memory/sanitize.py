from __future__ import annotations

import re
from functools import lru_cache

from sqlmodel import Session

from thenetwork.db.models import Memory

_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
_PHONE_RE = re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')

# NER entity types redacted via Presidio, mapped to the same bracket-token
# style as the deterministic regex strip.
_PRESIDIO_ENTITY_LABELS = {
    "PERSON": "[name]",
    "ORGANIZATION": "[org]",
    "LOCATION": "[location]",
}


def _strip_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text


@lru_cache(maxsize=1)
def _get_presidio_analyzer():
    """Build (and cache) a Presidio AnalyzerEngine, or None if unavailable.

    Presidio (plus its spacy model) is an optional dependency (the
    `pii-ner` extra) — some deploy environments can't reach the network to
    fetch the spacy model. Callers must treat None as "fall back to the
    regex-only strip", never crash.
    """
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError:
        return None
    try:
        return AnalyzerEngine()
    except Exception:
        # e.g. the spacy model isn't downloaded/available locally.
        return None


def _strip_pii_ner(text: str) -> str:
    """Apply Presidio NER redaction for names, orgs, and locations.

    Returns the input unchanged if Presidio (or its model) isn't available;
    this is a pure enhancement layered on top of `_strip_pii`, never a
    replacement for it.
    """
    analyzer = _get_presidio_analyzer()
    if analyzer is None:
        return text

    results = analyzer.analyze(
        text=text,
        entities=list(_PRESIDIO_ENTITY_LABELS),
        language="en",
    )
    # Redact right-to-left so earlier spans' offsets stay valid.
    for result in sorted(results, key=lambda r: r.start, reverse=True):
        token = _PRESIDIO_ENTITY_LABELS.get(result.entity_type)
        if token is None:
            continue
        text = text[: result.start] + token + text[result.end :]
    return text


def sanitize_memory(memory: Memory, session: Session) -> str:
    """Produce and persist a gist for a person-referencing memory.

    Runs a deterministic PII strip (emails, phone numbers) plus, when
    Presidio is installed and its model is available, NER-based redaction of
    person names, organizations, and locations. When Presidio isn't
    available this degrades gracefully to the regex-only strip so the
    function never hard-crashes on the optional dependency. Writes the
    result back as memory.gist. Call for any memory with refs before it is
    eligible for cross-user search (SEAL requirement).
    """
    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )
    gist = _strip_pii(memory.text)
    gist = _strip_pii_ner(gist)
    memory.gist = gist
    session.add(memory)
    session.flush()
    return gist


async def sanitize_memory_llm(memory: Memory, session: Session) -> str:
    """LLM-based sanitization: broader PII removal than the deterministic strip.

    Fixed system prompt, no tools, no external influence (SEAL layer 4 — see
    docs/security.md). Slower and costs an LLM call, so it is not the
    always-on default; see sanitize_memory_high_fidelity and
    settings.sanitize_llm_tier_enabled for how it's gated in. Beyond the
    deterministic regex + Presidio NER pass (names, emails, phones,
    addresses, orgs), this prompt also asks the model to catch what pattern
    matching structurally can't: social handles, URLs, and "quasi-identifying
    combinations" — otherwise-innocuous facts that, combined, single out one
    person (e.g. "the only Rust developer in Fargo").
    """
    from pydantic_ai import Agent
    from thenetwork.settings import get_settings

    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )

    s = get_settings()
    _sanitizer: Agent[None, str] = Agent(
        model=s.small_agent_model,
        system_prompt=(
            "You are a PII sanitizer. You will receive a memory about a person. "
            "Return a version with all personally-identifying information removed: "
            "replace names with [name], email addresses with [email], phone numbers "
            "with [phone], specific street addresses with [address], employers or "
            "other organizations with [org], social media handles or platform "
            "usernames (e.g. @someuser) with [handle], and URLs or links with "
            "[url]. Also watch for quasi-identifying combinations: a set of "
            "otherwise-innocuous facts that, taken together, would let a reader "
            "single out this one person (e.g. 'the only Rust developer in "
            "Fargo', or 'the CTO's college roommate who now lives in a town of "
            "2,000 people') — when you see one, generalize the specific detail "
            "(e.g. drop the town, broaden the role, widen the timeframe) so the "
            "combination no longer narrows to one identifiable individual. "
            "Keep factual content (skills, interests, context) that is not "
            "identifying, on its own or in combination. "
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
    """Produce and persist a SEAL-safe gist, using the LLM sanitizer when enabled.

    The LLM pass is an opt-in tier (settings.sanitize_llm_tier_enabled,
    default off — it's slower and costs a model call on every write) that
    catches quasi-identifiers and free-text PII the deterministic + NER pass
    can't pattern-match. When the tier is disabled, or the LLM pass fails for
    any reason, this falls back to the deterministic sanitizer so
    person-referencing memories always get a gist before cross-user search.
    """
    from thenetwork.settings import get_settings

    s = get_settings()
    if not s.sanitize_llm_tier_enabled:
        return sanitize_memory(memory, session)
    try:
        return await sanitize_memory_llm(memory, session)
    except Exception:
        return sanitize_memory(memory, session)
