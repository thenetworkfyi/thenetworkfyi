from __future__ import annotations

import re
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

# A platform handle names a real person outside this system, but it is an
# ordinary word to an NER model: Presidio classifies `mkly` as no supported
# entity at all, so it survives _strip_pii_ner and reaches a cross-user gist.
# The optional LLM tier is asked to catch handles, but it is opt-in and
# sanitize_text_high_fidelity falls back to the deterministic gist on any
# failure, so a handle must not depend on it. These patterns are structural:
# a bare word cannot be recognized in isolation, so each requires a profile
# URL, an `@` sigil, or a platform label, and only the identifier itself is
# replaced.
#
# A URL path segment and an `@` sigil are unambiguous. A platform label is
# not - "LinkedIn: Senior Engineer" is prose, not a username - so that form
# additionally requires the candidate to look like an identifier rather than
# a word: an explicit marker ("username: x"), identifier punctuation, or
# absence from the English lexicon already loaded for Presidio. Redacting
# ordinary role and description words would corrupt exactly the freeform
# content gists are embedded for.
_HANDLE_TOKEN = "[handle]"
_HANDLE_BODY = r"(?P<handle>[A-Za-z][A-Za-z0-9_.-]{2,38})"
_HANDLE_PLATFORMS = (
    "github",
    "gitlab",
    "bitbucket",
    "twitter",
    "instagram",
    "telegram",
    "signal",
    "discord",
    "mastodon",
    "bluesky",
    "bsky",
    "reddit",
    "tiktok",
    "twitch",
    "youtube",
    "linkedin",
    "slack",
    "keybase",
    "threads",
)
# Unambiguous forms: the identifier's position alone marks it as a handle.
# Profile URL: github.com/mkly, https://linkedin.com/in/mike-lay
_HANDLE_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://)?(?:www\.)?"
    r"(?:" + "|".join((*_HANDLE_PLATFORMS, "x")) + r")"
    r"\.(?:com|io|org|social|app|tv|me)/(?:in/|u/|user/|r/|@)?" + _HANDLE_BODY + r"\b"
)
# Bare sigil: @mkly. The lookbehind keeps this off an email's domain. The
# sigil is consumed with the identifier rather than left as a stray "@".
_HANDLE_SIGIL_PATTERN = re.compile(r"(?<![\w@./-])@" + _HANDLE_BODY + r"\b")

# Labelled: "GitHub: mkly", "twitter handle = @mkly", "my discord is mkly".
# Guarded by _is_handle_like because the label does not prove the candidate
# after it is an identifier.
_HANDLE_LABEL_PATTERN = re.compile(
    r"(?i)\b(?:" + "|".join(_HANDLE_PLATFORMS) + r")"
    r"(?:\s+(?P<marker>handle|username|user|profile|account|id))?"
    r"(?:\s*[:=]\s*|\s+is\s+)(?P<sigil>@)?" + _HANDLE_BODY + r"\b"
)

SANITIZER_SYSTEM_PROMPT = (
    "You are a PII sanitizer. You will receive freeform content that may "
    "be shown outside its owner's privacy boundary. Return a version with "
    "all personally-identifying information removed: replace names with "
    "[name], email addresses with [email], phone numbers with [phone], "
    "specific street addresses with [address], employers or other "
    "organizations with [org], social media handles, platform usernames, and "
    "account or screen names with [handle] - including a bare username "
    "written as an ordinary word, with no @ sigil, platform label, or link "
    "marking it as one - and URLs or links with [url]. Also watch for "
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


@lru_cache(maxsize=1)
def _english_vocab():
    """Return the spaCy vocab Presidio already loaded for its NER pass.

    Reuses the pinned en_core_web_lg model rather than shipping a second
    word list or loading a second pipeline. A missing model raises here for
    the same reason it raises in _get_presidio_analyzer: it is a deployment
    error, not grounds for a silent downgrade.
    """
    return _get_presidio_analyzer().nlp_engine.nlp["en"].vocab


def _is_handle_like(candidate: str, *, explicit: bool) -> bool:
    """Decide whether a platform-labelled candidate is an identifier or prose.

    "GitHub: mkly" is a username; "LinkedIn: Senior Engineer" is a job title
    that must survive into the gist. Three signals separate them, cheapest
    first: an explicit marker in the source text, identifier punctuation, and
    finally absence from the English lexicon.
    """
    if explicit:
        return True
    if any(char.isdigit() or char in "_-." for char in candidate):
        return True
    return all(
        _english_vocab()[part.lower()].is_oov
        for part in re.split(r"[_.-]", candidate)
        if part
    )


def _replace_handle(match: re.Match[str]) -> str:
    """Substitute only the identifier, keeping any label or host prefix."""
    return match.group(0)[: match.start("handle") - match.start(0)] + _HANDLE_TOKEN


def _replace_labelled_handle(match: re.Match[str]) -> str:
    explicit = bool(match.group("marker") or match.group("sigil"))
    if not _is_handle_like(match.group("handle"), explicit=explicit):
        return match.group(0)
    return _replace_handle(match)


def _strip_handles(text: str) -> str:
    """Replace platform handles and profile URLs with [handle].

    Runs before Presidio so the NER pass sees an already-neutralized span
    rather than a bare word it has no entity type for. Only the identifier is
    substituted; any platform label or host in the match is kept for search
    recall. Idempotent by construction - every pattern requires the
    identifier to start with a letter, so none can re-match `[handle]`.
    """
    text = _HANDLE_URL_PATTERN.sub(_replace_handle, text)
    text = _HANDLE_SIGIL_PATTERN.sub(lambda _match: _HANDLE_TOKEN, text)
    return _HANDLE_LABEL_PATTERN.sub(_replace_labelled_handle, text)


def sanitize_text(text: str) -> str:
    """Return a deterministic PII-stripped projection of freeform content.

    This is the shared SEAL boundary for freeform records that can cross a
    user boundary. Callers persist only the returned projection in searchable
    fields; the raw text remains confined to its owner-controlled record.
    """
    return _strip_pii_ner(_strip_handles(text))


def sanitize_memory(memory: Memory, session: Session) -> str:
    """Produce and persist a gist for a person-referencing memory.

    Runs mandatory Presidio redaction for person names, email addresses, and
    phone numbers, plus structural redaction of platform handles and profile
    URLs. Organizations and locations are kept so gists still carry
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
