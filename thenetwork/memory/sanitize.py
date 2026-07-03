from __future__ import annotations

from functools import lru_cache

from sqlmodel import Session

from thenetwork.db.models import Memory

# NER entity types redacted via Presidio, mapped to the same bracket-token
# style used throughout sanitized gists.
_PRESIDIO_ENTITY_LABELS = {
    "PERSON": "[name]",
    "EMAIL_ADDRESS": "[email]",
    "PHONE_NUMBER": "[phone]",
}


@lru_cache(maxsize=1)
def _get_presidio_analyzer():
    """Build and cache a Presidio AnalyzerEngine.

    Presidio is a required dependency for the deterministic sanitizer. If the
    package or its NLP model is unavailable, fail loudly so deploy setup cannot
    silently produce raw cross-user gists.
    """
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError as exc:
        raise RuntimeError("presidio-analyzer is required for memory sanitization") from exc
    try:
        return AnalyzerEngine()
    except Exception as exc:
        raise RuntimeError(
            "Presidio AnalyzerEngine could not start; install its required NLP model"
        ) from exc


def assert_presidio_ready() -> None:
    """Fail fast if the required Presidio analyzer cannot initialize."""
    _get_presidio_analyzer()


def _strip_pii_ner(text: str) -> str:
    """Apply Presidio redaction for names, email addresses, and phone numbers."""
    analyzer = _get_presidio_analyzer()
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

    Runs mandatory Presidio redaction for person names, email addresses, and
    phone numbers. Organizations and locations are kept so gists still carry
    useful search recall for companies and places. Writes the result back as
    memory.gist. Call for any memory with refs before it is eligible for
    cross-user search (SEAL requirement).
    """
    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )
    gist = _strip_pii_ner(memory.text)
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
    deterministic Presidio pass (names, emails, phones), this prompt also
    asks the model to catch what pattern
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
    catches quasi-identifiers and free-text PII the deterministic Presidio pass
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
