from __future__ import annotations

from functools import lru_cache

from sqlmodel import Session

from thenetwork.audit import audit_event
from thenetwork.db.models import Memory

MAX_SANITIZED_GIST_CHARS = 8_000
_SANITIZER_TRANSCRIPT_MARKERS = (
    "systempromptpart(",
    "userpromptpart(",
    "modelrequest(",
    "modelresponse(",
    "textpart(",
    "toolcallpart(",
    "toolreturnpart(",
    "<|assistant|>",
    "<|system|>",
    "you are a pii sanitizer",
    "return only the sanitized text",
)


class _UnsafeSanitizerOutput(ValueError):
    """The optional model returned content that cannot cross the SEAL."""


# NER entity types redacted via Presidio, mapped to the same bracket-token
# style used throughout sanitized gists.
_PRESIDIO_ENTITY_LABELS = {
    "PERSON": "[name]",
    "EMAIL_ADDRESS": "[email]",
    "PHONE_NUMBER": "[phone]",
}

SANITIZER_SYSTEM_PROMPT = (
    "You are a PII sanitizer. You will receive freeform content that may "
    "be shown outside its owner's privacy boundary. Return a version with "
    "all personally-identifying information removed: replace names with "
    "[name], email addresses with [email], phone numbers with [phone], "
    "specific street addresses with [address], employers or other "
    "organizations with [org], social media handles or platform usernames "
    "with [handle], and URLs or links with [url]. Also watch for "
    "quasi-identifying combinations: generalize details that together "
    "would single out one person. Keep non-identifying factual content "
    "useful for semantic matching. Return only the sanitized text."
)


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
        raise RuntimeError(
            "presidio-analyzer is required for memory sanitization"
        ) from exc
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


def sanitize_text(text: str) -> str:
    """Return a deterministic PII-stripped projection of freeform content.

    This is the shared SEAL boundary for freeform records that can cross a
    user boundary. Callers persist only the returned projection in searchable
    fields; the raw text remains confined to its owner-controlled record.
    """
    return _strip_pii_ner(text)


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
    gist = sanitize_text(memory.text)
    memory.gist = gist
    session.add(memory)
    session.flush()
    return gist


async def sanitize_text_llm(text: str) -> str:
    """Apply the fixed, tool-free high-fidelity sanitizer to freeform text."""
    from pydantic_ai import Agent
    from thenetwork.llm_observability import LLMWorkload
    from thenetwork.model_config import model_with_api_key
    from thenetwork.settings import get_settings

    s = get_settings()
    sanitizer: Agent[None, str] = Agent(
        model=model_with_api_key(
            s.small_agent_model,
            s.small_agent_api_key,
            s.model_request_timeout_seconds,
            workload=LLMWorkload.MEMORY_SANITIZER,
        ),
        system_prompt=SANITIZER_SYSTEM_PROMPT,
        output_type=str,
    )
    result = await sanitizer.run(text)
    return result.output


def _validate_llm_gist(candidate: str, deterministic_gist: str) -> str:
    """Return a bounded model gist or reject it for deterministic fallback."""
    if not isinstance(candidate, str) or not candidate.strip():
        raise _UnsafeSanitizerOutput("blank sanitizer output")

    max_chars = min(
        MAX_SANITIZED_GIST_CHARS,
        max(512, len(deterministic_gist) * 2),
    )
    if len(candidate) > max_chars:
        raise _UnsafeSanitizerOutput("expanded sanitizer output")

    lowered = candidate.casefold()
    if any(marker in lowered for marker in _SANITIZER_TRANSCRIPT_MARKERS):
        raise _UnsafeSanitizerOutput("transcript-shaped sanitizer output")

    # The optional model must not reintroduce a name, address, or phone that
    # the mandatory deterministic layer can identify.
    if sanitize_text(candidate) != candidate:
        raise _UnsafeSanitizerOutput("sanitizer output contains deterministic PII")
    return candidate


async def sanitize_text_high_fidelity(text: str) -> str:
    """Return a SEAL-safe gist, with mandatory Presidio fallback."""
    from thenetwork.settings import get_settings

    s = get_settings()
    deterministic_gist = sanitize_text(text)
    if not s.sanitize_llm_tier_enabled:
        return deterministic_gist
    try:
        # The optional model never sees the PII already caught by mandatory
        # Presidio and therefore cannot reproduce it in its output.
        candidate = await sanitize_text_llm(deterministic_gist)
        return _validate_llm_gist(candidate, deterministic_gist)
    except Exception as exc:
        audit_event("sanitize.tier_downgrade", error_type=type(exc).__name__)
        return deterministic_gist


async def sanitize_memory_llm(memory: Memory, session: Session) -> str:
    """LLM-based sanitization: broader PII removal than the deterministic strip.

    Fixed system prompt, no tools, no external influence (SEAL layer 4 - see
    docs/security.md). Slower and costs an LLM call, so it is not the
    always-on default; see sanitize_memory_high_fidelity and
    settings.sanitize_llm_tier_enabled for how it's gated in. Beyond the
    deterministic Presidio pass (names, emails, phones), this prompt also
    asks the model to catch what pattern
    matching structurally can't: social handles, URLs, and "quasi-identifying
    combinations" - otherwise-innocuous facts that, combined, single out one
    person (e.g. "the only Rust developer in Fargo").
    """
    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )

    deterministic_gist = sanitize_text(memory.text)
    candidate = await sanitize_text_llm(deterministic_gist)
    gist = _validate_llm_gist(candidate, deterministic_gist)
    memory.gist = gist
    session.add(memory)
    session.flush()
    return gist


async def sanitize_memory_high_fidelity(memory: Memory, session: Session) -> str:
    """Produce and persist a SEAL-safe gist, using the LLM sanitizer when enabled.

    The LLM pass is an opt-in tier (settings.sanitize_llm_tier_enabled,
    default off - it's slower and costs a model call on every write) that
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
    except Exception as exc:
        audit_event("sanitize.tier_downgrade", error_type=type(exc).__name__)
        return sanitize_memory(memory, session)
