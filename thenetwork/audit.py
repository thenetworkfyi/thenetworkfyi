"""PII-safe structured audit events for the agent execution lifecycle."""
from __future__ import annotations
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from typing import Iterator
from uuid import uuid4

LOGGER_NAME = "thenetwork.audit"
_logger = logging.getLogger(LOGGER_NAME)
_run_id: ContextVar[str | None] = ContextVar("thenetwork_audit_run_id", default=None)

_SAFE_FIELDS = frozenset({
    "action", "auto_submitted_present", "body_chars", "duration_ms", "error_type",
    "header_names", "html_present", "message_count", "outcome", "part_kinds",
    "query_chars", "reason", "recipient_id_present", "record_type", "refs_count",
    "result_count", "sender_known", "sender_present", "subject_chars", "tool_name",
    "top_k", "user_message_chars",
})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SAFE_CATEGORIES = {
    "action": frozenset({"delete", "insert", "lookup", "search"}),
    "outcome": frozenset({"error", "found", "not_found", "success"}),
    "reason": frozenset({"content_scan", "rate_limit"}),
    "record_type": frozenset({"memory", "person"}),
    "tool_name": frozenset({"dispatch_email", "escalate", "forget", "remember", "search"}),
}
_SAFE_HEADERS = frozenset({"auto-submitted", "from", "subject"})


def configure_audit_logging() -> None:
    """Emit audit JSON to stderr for worker/producer entrypoints."""
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def _safe_token(value: object) -> str:
    token = str(value)
    return token if _SAFE_TOKEN.fullmatch(token) else "unknown"


def _validate_value(name: str, value: object) -> object:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        if name in _SAFE_CATEGORIES:
            return value if value in _SAFE_CATEGORIES[name] else "unknown"
        return _safe_token(value)
    if isinstance(value, (list, tuple)):
        if name == "header_names":
            return [item for item in value if item in _SAFE_HEADERS]
        return [_safe_token(item) for item in value]
    raise TypeError(f"unsupported audit field type for {name!r}: {type(value).__name__}")


def audit_event(event: str, **fields: object) -> None:
    unknown = fields.keys() - _SAFE_FIELDS
    if unknown:
        raise ValueError(f"unsafe audit fields: {', '.join(sorted(unknown))}")
    payload: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": _safe_token(event),
    }
    run_id = _run_id.get()
    if run_id is not None:
        payload["run_id"] = run_id
    payload.update({name: _validate_value(name, value) for name, value in fields.items()})
    _logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


@contextmanager
def audit_run() -> Iterator[str]:
    current = _run_id.get()
    if current is not None:
        yield current
        return
    run_id = str(uuid4())
    token = _run_id.set(run_id)
    try:
        yield run_id
    finally:
        _run_id.reset(token)


@contextmanager
def audit_span(event: str, **fields: object) -> Iterator[None]:
    started = monotonic()
    audit_event(f"{event}.started", **fields)
    try:
        yield
    except Exception as exc:
        audit_event(f"{event}.completed", **fields, outcome="error",
                    error_type=type(exc).__name__,
                    duration_ms=round((monotonic() - started) * 1000, 3))
        raise
    else:
        audit_event(f"{event}.completed", **fields, outcome="success",
                    duration_ms=round((monotonic() - started) * 1000, 3))


def audit_model_trace(result: object) -> None:
    all_messages = getattr(result, "all_messages", None)
    messages = all_messages() if callable(all_messages) else []
    part_kinds: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", ()):
            part_kinds.append(getattr(part, "part_kind", type(part).__name__))
    audit_event("agent.model_trace", message_count=len(messages), part_kinds=part_kinds)
