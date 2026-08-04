"""PII-safe structured audit events for the agent execution lifecycle."""

from __future__ import annotations

import json
import logging
import re
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Iterator
from uuid import uuid4

import structlog

from thenetwork.security.log_redaction import (
    redact_structured_log,
    redact_structured_values,
)

LOGGER_NAME = "thenetwork.audit"
_run_id: ContextVar[str | None] = ContextVar("thenetwork_audit_run_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar(
    "thenetwork_audit_trace_id", default=None
)
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


_FOREIGN_LOG_METADATA_FIELDS = frozenset(
    {"logger", "level", "_record", "_from_structlog"}
)


def _redact_foreign_content(event_dict: dict) -> dict:
    metadata = {
        key: event_dict[key]
        for key in _FOREIGN_LOG_METADATA_FIELDS
        if key in event_dict
    }
    content = {
        key: value
        for key, value in event_dict.items()
        if key not in _FOREIGN_LOG_METADATA_FIELDS
    }
    return {**metadata, **redact_structured_log(content)}


def _redact_procrastinate_job(value: object) -> object:
    if not isinstance(value, dict):
        return redact_structured_values(value)
    job = dict(value)
    job.pop("call_string", None)
    if "task_kwargs" in job:
        job["task_kwargs"] = redact_structured_values(job["task_kwargs"])
    return job


def _procrastinate_log_event(event_dict: dict) -> dict:
    """Use Procrastinate's action/extras instead of its argument-bearing prose."""
    action = event_dict.get("action")
    if not isinstance(action, str):
        return _redact_foreign_content(event_dict)

    structured = dict(event_dict)
    structured["event"] = f"procrastinate.{action}"
    structured.pop("exc_info", None)
    structured.pop("stack_info", None)
    if "job" in structured:
        structured["job"] = _redact_procrastinate_job(structured["job"])
    if isinstance(structured.get("jobs"), list):
        structured["jobs"] = [
            _redact_procrastinate_job(job) for job in structured["jobs"]
        ]
    if "result" in structured:
        structured["result"] = redact_structured_values(structured["result"])
    record = event_dict.get("_record")
    if isinstance(record, logging.LogRecord) and record.exc_info:
        structured["exception"] = redact_structured_log(
            "".join(traceback.format_exception(*record.exc_info))
        )
    return structured


def _redact_foreign_log_event(
    logger: object, method_name: str, event_dict: dict
) -> dict:
    """Redact content-bearing foreign fields while preserving log metadata."""
    logger_name = event_dict.get("logger")
    if logger_name == LOGGER_NAME:
        return event_dict
    if isinstance(logger_name, str) and (
        logger_name == "procrastinate" or logger_name.startswith("procrastinate.")
    ):
        return _procrastinate_log_event(event_dict)

    return _redact_foreign_content(event_dict)


# Shared by both chains below so audit events and third-party logs (e.g.
# Procrastinate's job-lifecycle logging) end up in the same JSON shape.
_SHARED_PROCESSORS = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    # Procrastinate publishes its stable action/job metadata through LogRecord
    # extras. Copy them into the event dict before applying field-level policy.
    structlog.stdlib.ExtraAdder(),
    # Provider and library errors may include a rejected model response in the
    # record message. Redact the event dict before it reaches any stderr/JSONL
    # sink, including the foreign-stdlib logging bridge below.
    _redact_foreign_log_event,
    _iso_timestamp,
]
_JSON_RENDERER = structlog.processors.JSONRenderer(
    sort_keys=True, separators=(",", ":")
)

_logger = structlog.wrap_logger(
    logging.getLogger(LOGGER_NAME),
    processors=[*_SHARED_PROCESSORS, _JSON_RENDERER],
)

_SAFE_FIELDS = frozenset(
    {
        "action",
        "auth_result_mechanisms",
        "authserv_id",
        "auto_submitted_present",
        "attachment_count",
        "body_chars",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_status",
        "duration_ms",
        "agent_duration_ms",
        "agent_observed",
        "error_type",
        "estimated_cost_usd",
        "header_names",
        "html_present",
        "input_tokens",
        "intake_observed",
        "message_count",
        "model_duration_ms",
        "model_endpoint",
        "model_name",
        "model_provider",
        "model_provider_host",
        "model_request_count",
        "outcome",
        "output_tokens",
        "part_kinds",
        "process_duration_ms",
        "queue_duration_ms",
        "consent_state",
        "query_chars",
        "recent_memory_context_chars",
        "recent_memory_gist_count",
        "reason",
        "recipient_id_present",
        "recipient_count",
        "record_type",
        "rendering_mode",
        "refs_count",
        "result_count",
        "retry_count",
        "sender_authenticated",
        "sender_id_hash",
        "sender_known",
        "sender_present",
        "subject_chars",
        "tool_called",
        "tool_name",
        "tool_names",
        "tool_outcome",
        "tool_reason",
        "top_k",
        "total_duration_ms",
        "template_id",
        "trace_id",
        "http_method",
        "http_status",
        "user_message_chars",
        "unpriced_request_count",
        "usage_unavailable_request_count",
        "verdict",
        "workload",
    }
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
# model_name carries provider-returned data (e.g. OpenRouter's `vendor/model`
# ids), not trusted local config, so it keeps its own narrow validator rather
# than widening _SAFE_TOKEN - which guards every other non-category string
# field - for every caller. Same length bound and character set as
# _SAFE_TOKEN, plus `/`; still rejects whitespace, newlines, quotes, and
# control characters so nothing can corrupt or inject into the JSONL stream.
_SAFE_MODEL_NAME = re.compile(r"^[A-Za-z0-9_./:-]{1,80}$")
_SAFE_CATEGORIES = {
    "action": frozenset(
        {
            "ban",
            "clarify",
            "consent",
            "decline",
            "delete",
            "insert",
            "lookup",
            "pause",
            "propose",
            "resume",
            "revoke",
            "search",
            "unban",
            "update",
        }
    ),
    "outcome": frozenset(
        {
            "blocked",
            "error",
            "exists",
            "found",
            "not_found",
            "rate_limited",
            "rejected_already_registered",
            "rejected_forbidden",
            "rejected_unauthenticated",
            "replayed",
            "success",
        }
    ),
    "tool_outcome": frozenset(
        {
            "created",
            "cancelled",
            "already_cancelled",
            "already_enabled",
            "already_suppressed",
            "deferred",
            "deleted",
            "error",
            "escalated",
            "exists",
            "forbidden",
            "limited",
            "not_found",
            "no_action",
            "proposed",
            "replayed",
            "sent",
            "success",
            "suppressed",
            "resumed",
            "updated",
            "welcomed",
            "welcomed_and_escalated",
        }
    ),
    "tool_reason": frozenset(
        {
            "already_registered",
            "declined",
            "event_already_notified",
            "event_cancelled",
            "event_expired",
            "event_expiry_not_future",
            "event_not_considered",
            "event_not_found",
            "event_recommendations_stopped",
            "event_text_too_long",
            "event_version_changed",
            "introduced",
            "invalid_event_expiry",
            "invalid_person_id",
            "max_sends_per_run",
            "memory_text_too_long",
            "not_sender_memory",
            "one_consented",
            "outside_proactive_pair",
            "outside_event_trigger",
            "not_event_owner",
            "person_memory_limit_exceeded",
            "person_not_found",
            "proposed",
            "query_too_long",
            "recipient_consent_request_cap",
            "recipient_daily_cap",
            "recipient_not_found",
            "recipient_outstanding_request_cap",
            "registration_quota_exceeded",
            "revoked",
            "run_proposal_cap",
            "sanitization_failed",
            "self_introduction",
            "self_event",
            "sender_not_authenticated",
            "sender_declined_participation",
            "sender_not_registered",
            "sender_reply_daily_cap",
            "use_reply_to_sender",
            "welcome_daily_cap",
        }
    ),
    "reason": frozenset(
        {
            "admin_auth_failed",
            "body_empty",
            "body_oversize",
            "cc_only_recipient",
            "content_scan",
            "daily_token_budget_exhausted",
            "disposable_domain",
            "primary_intake_paused",
            "prompt_injection_detected",
            "invalid_summary",
            "memory_text_too_long",
            "new_sender_burst",
            "person_memory_limit_exceeded",
            "rate_limit",
            "recipient_not_found",
            "relay_forbidden",
            "relay_invalid",
            "established_sender_traffic",
            "multi_sender_campaign",
            "routine_variation",
            "scanner_error",
            "shared_body_pattern",
            "shared_domain_pattern",
            "unauthenticated_unknown_sender",
            "unusual_new_sender_volume",
            "banned",
        }
    ),
    "template_id": frozenset(
        {
            "consent_acknowledgment",
            "consent_already_declined",
            "consent_clarification",
            "consent_declined",
            "consent_request",
            "conversational",
            "event_recommendation",
            "first_contact_welcome",
            "infrastructure_rejection",
            "introduction",
            "introduction_relay",
        }
    ),
    "model_endpoint": frozenset(
        {"chat_completions", "embeddings", "responses", "other"}
    ),
    "workload": frozenset(
        {"email_agent", "memory_sanitizer", "abuse_judge", "embedding"}
    ),
    "model_provider": frozenset(
        {
            "anthropic",
            "bedrock",
            "cerebras",
            "cohere",
            "fireworks",
            "google-gla",
            "google-vertex",
            "groq",
            "huggingface",
            "mistral",
            "ollama",
            "openai",
            "openrouter",
            "other",
            "test",
            "xai",
        }
    ),
    "cost_status": frozenset({"estimated", "unavailable"}),
    "verdict": frozenset({"normal", "suspicious", "coordinated_abuse"}),
    "http_method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
    "record_type": frozenset(
        {
            "event",
            "event_recommendation",
            "event_suppression",
            "introduction_consent",
            "memory",
            "person",
            "primary_intake",
            "sent_email_memory",
        }
    ),
    "consent_state": frozenset(
        {"declined", "introduced", "one_consented", "proposed", "revoked"}
    ),
    "rendering_mode": frozenset({"html", "internal_plain", "plain_fallback"}),
    "tool_name": frozenset(
        {
            "escalate",
            "cancel_event",
            "create_event",
            "forget",
            "no_action",
            "propose_introduction",
            "reply_to_sender",
            "resume_event_recommendations",
            "register_person",
            "remember",
            "search",
            "search_events",
            "send_event_recommendation",
            "send_first_contact_welcome",
            "send_outreach",
            "stop_event_recommendations",
            "update_event",
        }
    ),
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
    root_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED_PROCESSORS,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _JSON_RENDERER,
            ],
        )
    )
    root_logger = logging.getLogger()
    root_logger.handlers = [root_handler]
    root_logger.setLevel(logging.WARNING)  # keep third-party libraries quiet by default

    # Procrastinate's job start/finish/retry/failure logs are worth surfacing
    # at INFO rather than vanishing under root's default WARNING threshold.
    logging.getLogger("procrastinate").setLevel(logging.INFO)
    # Loading the span classifier the redactor depends on emits transformers
    # progress and device warnings; keep them from recursively entering the
    # foreign-log redactor while it is still starting.
    logging.getLogger("transformers").setLevel(logging.ERROR)


class _AuditFileFilter(logging.Filter):
    """Optionally keep content-bearing model responses out of a JSONL sink."""

    def __init__(self, *, include_model_responses: bool) -> None:
        super().__init__()
        self.include_model_responses = include_model_responses

    def filter(self, record: logging.LogRecord) -> bool:
        if self.include_model_responses:
            return True
        try:
            payload = json.loads(record.getMessage())
        except (TypeError, ValueError):
            return True
        return payload.get("event") != "agent.model_response"


@contextmanager
def audit_jsonl_file(
    path: Path, *, include_model_responses: bool = True
) -> Iterator[None]:
    """Write selected audit events to JSONL without reconfiguring logging.

    Production audit keeps redacted model responses for diagnostics. Public
    simulation artifacts disable them because redaction removes identities and
    secrets, not all freeform owner-controlled event content.
    """
    stdlib_logger = logging.getLogger(LOGGER_NAME)
    previous_disabled = stdlib_logger.disabled
    previous_level = stdlib_logger.level
    previous_propagate = stdlib_logger.propagate
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_AuditFileFilter(include_model_responses=include_model_responses))
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


def _safe_model_name(value: object) -> str:
    token = str(value)
    return token if _SAFE_MODEL_NAME.fullmatch(token) else "unknown"


def _validate_value(name: str, value: object) -> object:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        if name in _SAFE_CATEGORIES:
            return value if value in _SAFE_CATEGORIES[name] else "unknown"
        if name == "model_name":
            return _safe_model_name(value)
        return _safe_token(value)
    if isinstance(value, (list, tuple)):
        if name == "header_names":
            return [item for item in value if item in _SAFE_HEADERS]
        if name == "tool_names":
            allowed = _SAFE_CATEGORIES["tool_name"]
            return [item if item in allowed else "unknown" for item in value]
        return [_safe_token(item) for item in value]
    raise TypeError(
        f"unsupported audit field type for {name!r}: {type(value).__name__}"
    )


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
    payload.update(
        {name: _validate_value(name, value) for name, value in fields.items()}
    )
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


def _model_response_payload(message: object) -> object:
    """Serialize one model response without ever falling back to repr()."""
    try:
        if is_dataclass(message) and not isinstance(message, type):
            return asdict(message)
        model_dump = getattr(message, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
    except Exception:
        pass
    return {"parts": "[redaction-unavailable]"}


def audit_model_trace(
    result: object, *, pseudonym_secret: str | bytes | None = None
) -> None:
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
    for message in messages:
        if getattr(message, "kind", None) != "response":
            continue
        payload = _audit_payload({"message_count": 1})
        _logger.info(
            "agent.model_response",
            **payload,
            response=redact_structured_log(
                _model_response_payload(message),
                pseudonym_secret=pseudonym_secret,
            ),
        )
