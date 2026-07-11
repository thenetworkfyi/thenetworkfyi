"""PII-safe structured audit events for the agent execution lifecycle."""
from __future__ import annotations
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Iterator
from uuid import uuid4

import structlog

LOGGER_NAME = "thenetwork.audit"
_run_id: ContextVar[str | None] = ContextVar("thenetwork_audit_run_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("thenetwork_audit_trace_id", default=None)
_sender_id_hash: ContextVar[str | None] = ContextVar(
    "thenetwork_audit_sender_id_hash",
    default=None,
)
_span_completion_fields: ContextVar[tuple[dict[str, object], ...]] = ContextVar(
    "thenetwork_audit_span_completion_fields",
    default=(),
)


def _iso_timestamp(logger: object, method_name: str, event_dict: dict) -> dict:
    event_dict.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    return event_dict


# Shared by both chains below so audit events and third-party logs (e.g.
# Procrastinate's job-lifecycle logging) end up in the same JSON shape.
_SHARED_PROCESSORS = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    _iso_timestamp,
]
_JSON_RENDERER = structlog.processors.JSONRenderer(sort_keys=True, separators=(",", ":"))

_logger = structlog.wrap_logger(
    logging.getLogger(LOGGER_NAME),
    processors=[*_SHARED_PROCESSORS, _JSON_RENDERER],
)

_SAFE_FIELDS = frozenset({
    "action", "auth_result_mechanisms", "authserv_id",
    "auto_submitted_present", "body_chars", "duration_ms", "error_type",
    "header_names", "html_present", "message_count", "outcome", "part_kinds",
    "consent_state", "query_chars", "reason", "recipient_id_present", "record_type", "refs_count",
    "result_count", "sender_authenticated", "sender_id_hash", "sender_known",
    "sender_present", "subject_chars", "tool_called", "tool_name", "tool_names",
    "tool_outcome", "tool_reason", "top_k", "trace_id", "user_message_chars",
})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SAFE_CATEGORIES = {
    "action": frozenset({
        "ban", "clarify", "consent", "delete", "insert", "lookup", "propose", "revoke",
        "search", "unban",
    }),
    "outcome": frozenset({
        "blocked", "error", "exists", "found", "not_found", "rate_limited",
        "rejected_already_registered", "rejected_forbidden",
        "rejected_unauthenticated", "success",
    }),
    "tool_outcome": frozenset({
        "created", "deleted", "error", "escalated", "exists", "forbidden",
        "limited", "not_found", "proposed", "sent", "success", "suppressed",
        "welcomed", "welcomed_and_escalated",
    }),
    "tool_reason": frozenset({
        "already_registered", "max_sends_per_run", "memory_text_too_long",
        "not_sender_memory", "person_memory_limit_exceeded",
        "query_too_long", "sanitization_failed",
        "recipient_daily_cap", "recipient_not_found",
        "registration_quota_exceeded", "sender_not_authenticated",
        "sender_reply_daily_cap", "person_not_found", "invalid_person_id",
        "self_introduction", "use_reply_to_sender",
    }),
    "reason": frozenset({
        "body_empty", "body_oversize", "content_scan", "rate_limit", "scanner_error",
        "unauthenticated_unknown_sender", "banned",
    }),
    "record_type": frozenset({"introduction_consent", "memory", "person"}),
    "consent_state": frozenset({"introduced", "one_consented", "proposed", "revoked"}),
    "tool_name": frozenset({
        "escalate", "forget", "propose_introduction", "reply_to_sender",
        "register_person", "remember", "search",
        "send_outreach",
    }),
}
_SAFE_HEADERS = frozenset({"auto-submitted", "from", "subject"})


def configure_audit_logging() -> None:
    """Emit audit JSON to stderr, and route everything else's stdlib logging
    (Procrastinate's job-lifecycle logs, any other library) through the same
    JSON shape so `docker compose logs | jq` sees one consistent schema
    regardless of source.
    """
    stdlib_logger = logging.getLogger(LOGGER_NAME)
    if not stdlib_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        stdlib_logger.addHandler(handler)
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.propagate = False

    # Non-structlog (plain stdlib) log records land here via the standard
    # foreign_pre_chain/ProcessorFormatter bridge, rendered to the same JSON.
    root_handler = logging.StreamHandler()
    root_handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, _JSON_RENDERER],
    ))
    root_logger = logging.getLogger()
    root_logger.handlers = [root_handler]
    root_logger.setLevel(logging.WARNING)  # keep third-party libraries quiet by default

    # Procrastinate's job start/finish/retry/failure logs are worth surfacing
    # at INFO rather than vanishing under root's default WARNING threshold.
    logging.getLogger("procrastinate").setLevel(logging.INFO)


@contextmanager
def audit_jsonl_file(path: Path) -> Iterator[None]:
    """Write audit events to one JSONL file without reconfiguring global logging."""
    stdlib_logger = logging.getLogger(LOGGER_NAME)
    previous_disabled = stdlib_logger.disabled
    previous_level = stdlib_logger.level
    previous_propagate = stdlib_logger.propagate
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    stdlib_logger.addHandler(handler)
    stdlib_logger.disabled = False
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.propagate = False
    try:
        yield
    finally:
        stdlib_logger.removeHandler(handler)
        handler.close()
        stdlib_logger.disabled = previous_disabled
        stdlib_logger.setLevel(previous_level)
        stdlib_logger.propagate = previous_propagate


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
        if name == "tool_names":
            allowed = _SAFE_CATEGORIES["tool_name"]
            return [item if item in allowed else "unknown" for item in value]
        return [_safe_token(item) for item in value]
    raise TypeError(f"unsupported audit field type for {name!r}: {type(value).__name__}")


def _audit_payload(fields: dict[str, object]) -> dict[str, object]:
    unknown = fields.keys() - _SAFE_FIELDS
    if unknown:
        raise ValueError(f"unsafe audit fields: {', '.join(sorted(unknown))}")
    payload: dict[str, object] = {}
    run_id = _run_id.get()
    if run_id is not None:
        payload["run_id"] = run_id
    trace_id = _trace_id.get()
    if trace_id is not None and "trace_id" not in fields:
        payload["trace_id"] = _validate_value("trace_id", trace_id)
    sender_id_hash = _sender_id_hash.get()
    if sender_id_hash is not None and "sender_id_hash" not in fields:
        payload["sender_id_hash"] = _validate_value("sender_id_hash", sender_id_hash)
    payload.update({name: _validate_value(name, value) for name, value in fields.items()})
    return payload


def audit_event(event: str, **fields: object) -> None:
    payload = _audit_payload(fields)
    is_error = payload.get("outcome") == "error" or bool(payload.get("error_type"))
    log_method = _logger.error if is_error else _logger.info
    log_method(_safe_token(event), **payload)


def audit_warning_event(event: str, **fields: object) -> None:
    _logger.warning(_safe_token(event), **_audit_payload(fields))


def audit_span_completion(**fields: object) -> None:
    """Attach safe fields to the innermost active audit_span completion event."""
    stack = _span_completion_fields.get()
    if not stack:
        return
    unknown = fields.keys() - _SAFE_FIELDS
    if unknown:
        raise ValueError(f"unsafe audit fields: {', '.join(sorted(unknown))}")
    stack[-1].update(fields)


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
def audit_trace(trace_id: str | None) -> Iterator[None]:
    if trace_id is None:
        yield
        return
    token = _trace_id.set(trace_id)
    try:
        yield
    finally:
        _trace_id.reset(token)


@contextmanager
def audit_sender(sender_id_hash: str | None) -> Iterator[None]:
    if sender_id_hash is None:
        yield
        return
    token = _sender_id_hash.set(sender_id_hash)
    try:
        yield
    finally:
        _sender_id_hash.reset(token)


@contextmanager
def audit_span(event: str, **fields: object) -> Iterator[None]:
    started = monotonic()
    completion_fields: dict[str, object] = {}
    token = _span_completion_fields.set(
        (*_span_completion_fields.get(), completion_fields)
    )
    audit_event(f"{event}.started", **fields)
    try:
        yield
    except Exception as exc:
        audit_event(
            f"{event}.completed",
            **fields,
            **completion_fields,
            outcome="error",
            error_type=type(exc).__name__,
            duration_ms=round((monotonic() - started) * 1000, 3),
        )
        raise
    else:
        audit_event(
            f"{event}.completed",
            **fields,
            **completion_fields,
            outcome="success",
            duration_ms=round((monotonic() - started) * 1000, 3),
        )
    finally:
        _span_completion_fields.reset(token)


def audit_model_trace(result: object) -> None:
    all_messages = getattr(result, "all_messages", None)
    messages = all_messages() if callable(all_messages) else []
    part_kinds: list[str] = []
    tool_names: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", ()):
            part_kind = getattr(part, "part_kind", type(part).__name__)
            part_kinds.append(part_kind)
            if part_kind == "tool-call":
                tool_names.append(getattr(part, "tool_name", "unknown"))
    audit_event(
        "agent.model_trace",
        message_count=len(messages),
        part_kinds=part_kinds,
        tool_names=tool_names,
    )
