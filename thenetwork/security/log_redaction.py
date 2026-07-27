"""Fail-closed redaction for structured data that may be written to logs.

The same local span classifier that produces memory gists
(:mod:`thenetwork.memory.sanitize`) does the labelling here; only the policy
differs. A gist is a search projection that deliberately keeps dates, places,
and organizations for recall, whereas a diagnostic model-response log has no
recall requirement and should shed every identifying or credential-bearing
string it can before it leaves process memory. So this module keeps its own,
broader allow-list over the same taxonomy - and, unlike the gist path, it
pseudonymizes rather than flattens the types operators correlate on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any


_PSEUDONYM_CONTEXT = b"thenetwork.log_redaction.v1\0"
_PSEUDONYM_BYTES = 12
_FAIL_CLOSED = "[redaction-unavailable]"

# Classifier label -> the entity type this module reports.
#
# Every label in the taxonomy is redacted here, including `private_date`, which
# the gist path deliberately keeps. A log line has no perishability signal to
# preserve, and a date beside a name is a quasi-identifier.
_ENTITY_TYPES = {
    "private_person": "PERSON",
    "private_email": "EMAIL_ADDRESS",
    "private_phone": "PHONE_NUMBER",
    "private_address": "LOCATION",
    "private_url": "URL",
    "private_date": "DATE_TIME",
    "account_number": "APPLICATION_IDENTIFIER",
    "secret": "SECRET",
}

# Types that get a keyed pseudonym instead of a bare placeholder, so operators
# can correlate a repeated token across records without it being reversible.
# Values here are machine identifiers whose repetition is the diagnostic
# signal; a person's name is not, and gets a flat placeholder.
_STABLE_ENTITY_TYPES = frozenset({"APPLICATION_IDENTIFIER", "SECRET", "URL"})


class LogRedactionError(RuntimeError):
    """Raised internally when the log redactor cannot safely initialize."""


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


def _spans(text: str) -> list[tuple[int, int, str]]:
    """Return classifier spans for this module's allow-list, as entity types."""
    from thenetwork.memory.sanitize import classify_spans

    try:
        merged = classify_spans(text, _ENTITY_TYPES)
    except Exception as exc:
        raise LogRedactionError("log redaction classifier failed") from exc

    return [
        (start, end, _ENTITY_TYPES[label])
        for start, end, label in merged
        if 0 <= start < end <= len(text)
    ]


def redact_text(value: str, *, pseudonym_secret: str | bytes | None = None) -> str:
    """Redact a single untrusted string.

    Overlapping detections are coalesced before replacement so no fragment of a
    longer sensitive match can survive merely because another span covered an
    inner range first.
    """
    if not value.strip():
        return value
    spans = _spans(value)
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
