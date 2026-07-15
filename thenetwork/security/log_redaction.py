"""Fail-closed redaction for structured data that may be written to logs.

This is deliberately separate from :mod:`thenetwork.memory.sanitize`.  Memory
gists are a search projection with narrowly chosen replacements; diagnostic
model-response logs instead need to remove every potentially identifying or
credential-bearing string before it leaves process memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from typing import Any


_PSEUDONYM_CONTEXT = b"thenetwork.log_redaction.v1\0"
_PSEUDONYM_BYTES = 12
_FAIL_CLOSED = "[redaction-unavailable]"

# These patterns cover values which are often missed by general NER models but
# are particularly unsafe in a full model trace.  They are also registered as
# Presidio recognizers below; keeping local spans makes the guarantee explicit
# when a test double or a future Presidio model does not return them.
_CUSTOM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "EMAIL_ADDRESS",
        re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])"),
    ),
    (
        "INTRO_TOKEN",
        re.compile(r"\[intro:[0-9a-f]{8}-[0-9a-f-]{27,}\]", re.I),
    ),
    (
        "URL",
        re.compile(r"\bhttps?://[^\s<>'\"]+", re.I),
    ),
    (
        "SECRET",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
            r"password|secret)\s*[:=]\s*(?:['\"])?[^\s,'\"}\]]+"
        ),
    ),
    ("SECRET", re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b")),
    ("SECRET", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "APPLICATION_IDENTIFIER",
        re.compile(
            r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})\b",
            re.I,
        ),
    ),
    (
        "APPLICATION_IDENTIFIER",
        re.compile(
            r"\b(?:user|usr|person|memory|mem|message|msg|thread|request|"
            r"run|trace|conversation|conv)_[A-Za-z0-9][A-Za-z0-9_-]{5,}\b",
            re.I,
        ),
    ),
)

_STABLE_ENTITY_TYPES = frozenset(
    {"APPLICATION_IDENTIFIER", "INTRO_TOKEN", "SECRET", "URL"}
)


class LogRedactionError(RuntimeError):
    """Raised internally when the log redactor cannot safely initialize."""


def _pattern_recognizers() -> list[object]:
    """Build Presidio recognizers for values absent from its stock registry."""
    try:
        from presidio_analyzer import Pattern, PatternRecognizer
    except ImportError as exc:  # pragma: no cover - exercised via initializer
        raise LogRedactionError(
            "presidio-analyzer is required for log redaction"
        ) from exc

    return [
        PatternRecognizer(
            supported_entity=entity_type,
            patterns=[
                Pattern(
                    name=f"thenetwork_{entity_type.lower()}",
                    regex=pattern.pattern,
                    score=0.9,
                )
            ],
        )
        for entity_type, pattern in _CUSTOM_PATTERNS
    ]


@lru_cache(maxsize=1)
def _get_log_analyzer() -> object:
    """Create the broad Presidio analyzer used only for diagnostic logging."""
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError as exc:
        raise LogRedactionError(
            "presidio-analyzer is required for log redaction"
        ) from exc

    try:
        analyzer = AnalyzerEngine()
        for recognizer in _pattern_recognizers():
            analyzer.registry.add_recognizer(recognizer)
        return analyzer
    except LogRedactionError:
        raise
    except Exception as exc:
        raise LogRedactionError("Presidio log redactor could not initialize") from exc


def _pseudonym(value: str, entity_type: str, secret: str | bytes | None) -> str:
    """Return a keyed stable token, or a non-correlatable replacement.

    A missing key intentionally does not fall back to an unkeyed hash.  That
    would make common values reversible by dictionary lookup.
    """
    if not secret:
        return f"[{entity_type.lower()}]"
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    digest = hmac.digest(
        key,
        _PSEUDONYM_CONTEXT
        + entity_type.encode("ascii")
        + b"\0"
        + value.encode("utf-8"),
        hashlib.sha256,
    )
    token = (
        base64.urlsafe_b64encode(digest[:_PSEUDONYM_BYTES]).decode("ascii").rstrip("=")
    )
    return f"[{entity_type.lower()}:log_v1_{token}]"


def _replacement(value: str, entity_type: str, secret: str | bytes | None) -> str:
    normalized = entity_type.upper()
    if normalized in _STABLE_ENTITY_TYPES:
        return _pseudonym(value, normalized, secret)
    return f"[{normalized.lower()}]"


def _spans(text: str, analyzer: object) -> list[tuple[int, int, str]]:
    """Return the union of broad Presidio and local custom-recognizer spans."""
    try:
        results = analyzer.analyze(text=text, language="en")  # type: ignore[attr-defined]
    except Exception as exc:
        raise LogRedactionError("Presidio log redaction failed") from exc

    found: list[tuple[int, int, str]] = []
    for result in results:
        start, end = int(result.start), int(result.end)
        if 0 <= start < end <= len(text):
            found.append((start, end, str(result.entity_type).upper()))
    for entity_type, pattern in _CUSTOM_PATTERNS:
        found.extend(
            (match.start(), match.end(), entity_type)
            for match in pattern.finditer(text)
        )
    return found


def redact_text(value: str, *, pseudonym_secret: str | bytes | None = None) -> str:
    """Redact a single untrusted string with broad Presidio coverage.

    Overlapping detections are coalesced before replacement so no fragment of a
    longer sensitive match can survive merely because another recognizer found
    an inner span first.
    """
    analyzer = _get_log_analyzer()
    spans = _spans(value, analyzer)
    if not spans:
        return value

    # Prefer a sensitive type and then the longest member when a connected
    # group of spans overlaps.  Replacing the entire union cannot reveal a
    # partial email, URL, token, or identifier.
    spans.sort(key=lambda item: (item[0], item[1]))
    groups: list[list[tuple[int, int, str]]] = []
    for span in spans:
        if not groups or span[0] > max(item[1] for item in groups[-1]):
            groups.append([span])
        else:
            groups[-1].append(span)

    pieces: list[str] = []
    cursor = 0
    for group in groups:
        start = min(item[0] for item in group)
        end = max(item[1] for item in group)
        entity_type = max(
            group,
            key=lambda item: (item[2] in _STABLE_ENTITY_TYPES, item[1] - item[0]),
        )[2]
        pieces.append(value[cursor:start])
        pieces.append(_replacement(value[start:end], entity_type, pseudonym_secret))
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _fail_closed(value: Any) -> Any:
    """Preserve only safe structure if redaction cannot run at all."""
    if isinstance(value, str):
        return _FAIL_CLOSED
    if isinstance(value, Mapping):
        return {
            f"[redacted-key-{index}]": _fail_closed(item)
            for index, item in enumerate(value.values())
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_fail_closed(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _FAIL_CLOSED


def _redact_value(value: Any, pseudonym_secret: str | bytes | None) -> Any:
    if isinstance(value, str):
        return redact_text(str(value), pseudonym_secret=pseudonym_secret)
    if isinstance(value, Mapping):
        return {
            redact_text(str(key), pseudonym_secret=pseudonym_secret): _redact_value(
                item, pseudonym_secret
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact_value(item, pseudonym_secret) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _redact_value(asdict(value), pseudonym_secret)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _redact_value(model_dump(mode="json"), pseudonym_secret)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # repr() can contain arbitrary model output, so never write it directly.
    return _FAIL_CLOSED


def redact_structured_log(
    value: Any, *, pseudonym_secret: str | bytes | None = None
) -> Any:
    """Recursively redact a model-response structure before logging it.

    If initialization or any redaction operation fails, no raw string is
    returned.  Callers may supply a server-side key to retain stable opaque
    correlation for tokens, URLs, secrets, and application identifiers.
    """
    try:
        return _redact_value(value, pseudonym_secret)
    except Exception:
        return _fail_closed(value)


def redact_structured_values(
    value: Any, *, pseudonym_secret: str | bytes | None = None
) -> Any:
    """Redact values in a schema-controlled artifact while retaining its keys.

    Simulation artifacts have fixed, application-owned keys that are useful to
    scorers and reviewers. Their values remain untrusted persona or model
    content, so this deliberately differs from ``redact_structured_log``.
    """
    try:
        return _redact_values_preserving_keys(value, pseudonym_secret)
    except Exception:
        return _fail_closed(value)


def _redact_values_preserving_keys(
    value: Any, pseudonym_secret: str | bytes | None
) -> Any:
    if isinstance(value, str):
        return redact_text(str(value), pseudonym_secret=pseudonym_secret)
    if isinstance(value, Mapping):
        return {
            str(key): _redact_values_preserving_keys(item, pseudonym_secret)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _redact_values_preserving_keys(item, pseudonym_secret) for item in value
        ]
    if is_dataclass(value) and not isinstance(value, type):
        return _redact_values_preserving_keys(asdict(value), pseudonym_secret)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _redact_values_preserving_keys(model_dump(mode="json"), pseudonym_secret)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _FAIL_CLOSED
