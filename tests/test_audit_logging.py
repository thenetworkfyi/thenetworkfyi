"""Structured audit coverage without creating a PII-at-rest side channel."""

from __future__ import annotations

import io
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.audit import (
    LOGGER_NAME,
    audit_event,
    audit_jsonl_file,
    audit_run,
    audit_warning_event,
)


@pytest.fixture(autouse=True)
def _empty_recent_sender_memory_context(monkeypatch):
    """Audit tests inject no DB history unless they explicitly exercise it."""
    from thenetwork.memory.recent_context import RecentSenderMemoryContext

    monkeypatch.setattr(
        "thenetwork.agent.core.load_recent_sender_memory_context",
        lambda *_args, **_kwargs: RecentSenderMemoryContext(),
    )
    monkeypatch.setattr(
        "thenetwork.worker.producer.is_primary_intake_paused", lambda: False
    )


def _events(caplog) -> list[dict]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


class _LogAnalyzer:
    def analyze(self, *, text, language):
        matches = []
        for value, entity_type in (
            ("Alice Example", "PERSON"),
            ("alice@example.test", "EMAIL_ADDRESS"),
        ):
            start = text.find(value)
            while start >= 0:
                matches.append(
                    SimpleNamespace(
                        start=start,
                        end=start + len(value),
                        entity_type=entity_type,
                    )
                )
                start = text.find(value, start + len(value))
        return matches


def _format_foreign_log(
    *,
    logger_name: str,
    message: str,
    extra: dict,
    exc_info=None,
) -> dict:
    from thenetwork import audit

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        audit.structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=audit._SHARED_PROCESSORS,
            processors=[
                audit.structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                audit._JSON_RENDERER,
            ],
        )
    )
    logger = logging.getLogger(logger_name)
    previous_handlers = logger.handlers[:]
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        log_method = logger.error if exc_info else logger.info
        log_method(message, extra=extra, exc_info=exc_info)
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        handler.close()
    return json.loads(stream.getvalue())


def _procrastinate_job_extra(*, action: str, result=None) -> dict:
    timestamp = 1784614500.0
    task_name = "thenetwork.worker.abuse_judge.judge_primary_email_abuse"
    task_kwargs = {
        "timestamp": timestamp,
        "sender_email": "alice@example.test",
        "body": "Private note from Alice Example",
    }
    extra = {
        "action": action,
        "worker": {
            "name": None,
            "worker_id": 7,
            "job_id": 35,
            "queues": [],
        },
        "job": {
            "id": 35,
            "status": "doing",
            "queue": "default",
            "priority": 0,
            "task_name": task_name,
            "task_kwargs": task_kwargs,
            "scheduled_at": None,
            "attempts": 1,
            "worker_id": 7,
            "call_string": f"{task_name}[35](timestamp={timestamp})",
        },
        "start_timestamp": timestamp,
        "duration": 4.992,
    }
    if result is not None:
        extra["result"] = result
    return extra


def test_procrastinate_start_log_preserves_metadata_and_redacts_only_job_content(
    monkeypatch,
):
    monkeypatch.setattr(
        "thenetwork.security.log_redaction._get_log_analyzer", _LogAnalyzer
    )
    event = _format_foreign_log(
        logger_name="procrastinate.worker.worker",
        message=(
            "Starting job "
            "thenetwork.worker.abuse_judge.judge_primary_email_abuse[35]"
            "(timestamp=1784614500.0, sender_email='alice@example.test')"
        ),
        extra=_procrastinate_job_extra(action="start_job"),
    )

    serialized = json.dumps(event)
    assert event["event"] == "procrastinate.start_job"
    assert event["logger"] == "procrastinate.worker.worker"
    assert event["level"] == "info"
    assert event["action"] == "start_job"
    assert event["job"]["task_name"] == (
        "thenetwork.worker.abuse_judge.judge_primary_email_abuse"
    )
    assert event["job"]["task_kwargs"]["timestamp"] == 1784614500.0
    assert event["start_timestamp"] == 1784614500.0
    assert "call_string" not in event["job"]
    assert "[url]er" not in serialized
    assert "[phone_number]" not in serialized
    assert "alice@example.test" not in serialized
    assert "Alice Example" not in serialized


def test_procrastinate_success_log_preserves_completion_metadata(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.security.log_redaction._get_log_analyzer", _LogAnalyzer
    )
    extra = _procrastinate_job_extra(
        action="job_success", result="Reply for alice@example.test"
    )
    extra["end_timestamp"] = 1784614504.992
    event = _format_foreign_log(
        logger_name="procrastinate.worker.worker",
        message=(
            "Job thenetwork.worker.tasks.process_email[35]"
            "(sender_email='alice@example.test') ended with status: Success, "
            "lasted 4.992 s - Result: Reply for alice@example.test"
        ),
        extra=extra,
    )

    serialized = json.dumps(event)
    assert event["event"] == "procrastinate.job_success"
    assert event["logger"] == "procrastinate.worker.worker"
    assert event["level"] == "info"
    assert event["job"]["id"] == 35
    assert event["job"]["task_name"] == (
        "thenetwork.worker.abuse_judge.judge_primary_email_abuse"
    )
    assert event["start_timestamp"] == 1784614500.0
    assert event["end_timestamp"] == 1784614504.992
    assert event["duration"] == 4.992
    assert "[url]er" not in serialized
    assert "[phone_number]" not in serialized
    assert "alice@example.test" not in serialized


def test_procrastinate_failure_log_redacts_result_and_exception_content(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.security.log_redaction._get_log_analyzer", _LogAnalyzer
    )
    private_error = "Delivery failed for alice@example.test and Alice Example"
    try:
        raise RuntimeError(private_error)
    except RuntimeError:
        exc_info = sys.exc_info()

    event = _format_foreign_log(
        logger_name="procrastinate.worker.worker",
        message=(
            "Job thenetwork.worker.tasks.process_email[36]"
            "(sender_email='alice@example.test') ended with status: Error"
        ),
        extra=_procrastinate_job_extra(
            action="job_error", result="Reply for alice@example.test"
        ),
        exc_info=exc_info,
    )

    serialized = json.dumps(event)
    assert event["event"] == "procrastinate.job_error"
    assert event["logger"] == "procrastinate.worker.worker"
    assert event["level"] == "error"
    assert event["job"]["id"] == 35
    assert event["duration"] == 4.992
    assert "alice@example.test" not in serialized
    assert "Alice Example" not in serialized
    assert "RuntimeError" in event["exception"]


def _mock_sender_lookup(sender_id: str | None) -> MagicMock:
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = None
    mock_session.exec.return_value.first.return_value = sender_id
    return mock_session


def _tool_ctx(
    *,
    sender_email: str = "alice@example.com",
    sender_user_id: str | None = None,
    sender_authenticated: bool = True,
):
    from thenetwork.agent.deps import AgentDeps
    from thenetwork.settings import Settings

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    ctx = SimpleNamespace(
        deps=AgentDeps(
            settings=Settings(
                agent_model="test:model",
                small_agent_model="test:model",
                embed_model="test:embed",
            ),
            sender_email=sender_email,
            sender_user_id=sender_user_id,
            sender_authenticated=sender_authenticated,
            session_factory=lambda: mock_session,
        )
    )
    return ctx, mock_session


def _tool_completed_event(events: list[dict], tool_name: str) -> dict:
    return next(
        event
        for event in events
        if event["event"] == "agent.tool.completed" and event["tool_name"] == tool_name
    )


def _database_action_event(events: list[dict], *, record_type: str) -> dict:
    return next(
        event
        for event in events
        if event["event"] == "database.action" and event["record_type"] == record_type
    )


def test_audit_events_are_json_and_correlated(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with audit_run():
        audit_event("test.event", message_count=2, outcome="success")
        audit_event("test.next", sender_known=True)

    events = _events(caplog)
    assert [event["event"] for event in events] == ["test.event", "test.next"]
    assert events[0]["run_id"] == events[1]["run_id"]
    assert events[0]["timestamp"].endswith("+00:00")


def test_audit_jsonl_file_reenables_disabled_logger_and_restores_it(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    logger = logging.getLogger(LOGGER_NAME)
    previous_disabled = logger.disabled
    logger.disabled = True

    try:
        with audit_jsonl_file(audit_path):
            audit_event("test.event", message_count=1)

        assert json.loads(audit_path.read_text())["event"] == "test.event"
        assert logger.disabled is True
    finally:
        logger.disabled = previous_disabled


def test_audit_jsonl_file_can_exclude_content_bearing_model_responses(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    logger = logging.getLogger(LOGGER_NAME)

    with audit_jsonl_file(audit_path, include_model_responses=False):
        audit_event("agent.tool.completed", tool_name="create_event", outcome="success")
        logger.info(
            json.dumps(
                {
                    "event": "agent.model_response",
                    "response": {"parts": [{"args": "RAW EVENT CONTENT"}]},
                }
            )
        )

    artifact = audit_path.read_text()
    assert "agent.tool.completed" in artifact
    assert "agent.model_response" not in artifact
    assert "RAW EVENT CONTENT" not in artifact


def test_audit_api_rejects_content_bearing_fields():
    with pytest.raises(ValueError, match="unsafe audit fields"):
        audit_event("unsafe", email="person@example.com", body="private")


def test_audit_categories_replace_arbitrary_values(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    audit_event("worker.message_rejected", reason="private_secret")

    assert _events(caplog)[0]["reason"] == "unknown"
    assert "private_secret" not in caplog.records[0].message


@pytest.mark.asyncio
async def test_no_action_output_has_exact_audit_name_and_outcome(caplog):
    from thenetwork.agent.tools import no_action

    ctx, _session = _tool_ctx()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    result = await no_action(ctx, "private reason that must not be logged")

    assert result == ""
    events = _events(caplog)
    assert [event["event"] for event in events] == [
        "agent.tool.started",
        "agent.tool.completed",
    ]
    assert {event["tool_name"] for event in events} == {"no_action"}
    assert events[-1]["tool_outcome"] == "no_action"
    assert "private reason" not in "\n".join(
        record.message for record in caplog.records
    )


def test_audit_event_level_split_error_vs_expected_negative_outcomes(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    audit_event("test.error_outcome", outcome="error")
    audit_event("test.error_type_only", error_type="ValueError")
    audit_event("test.rate_limited", outcome="rate_limited")
    audit_event("test.rejected", outcome="rejected_forbidden")
    audit_event("test.not_found", outcome="not_found")
    audit_event("test.tool_limited", tool_outcome="limited")

    levels_by_event = {
        record.message and json.loads(record.message)["event"]: record.levelname
        for record in caplog.records
    }
    assert levels_by_event["test.error_outcome"] == "ERROR"
    assert levels_by_event["test.error_type_only"] == "ERROR"
    assert levels_by_event["test.rate_limited"] == "INFO"
    assert levels_by_event["test.rejected"] == "INFO"
    assert levels_by_event["test.not_found"] == "INFO"
    assert levels_by_event["test.tool_limited"] == "INFO"


def test_audit_warning_event_uses_warning_level(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    audit_warning_event(
        "test.warning",
        authserv_id="mx.example.com",
        auth_result_mechanisms=["arc", "x-provider"],
    )

    event = _events(caplog)[0]
    assert event["event"] == "test.warning"
    assert event["authserv_id"] == "mx.example.com"
    assert event["auth_result_mechanisms"] == ["arc", "x-provider"]
    assert caplog.records[0].levelname == "WARNING"


def test_audit_trace_correlates_events_without_content(caplog):
    from thenetwork.audit import audit_sender, audit_trace

    trace_id = "2d24f8a9-c332-4e3f-85af-b1785e9ce4ab"
    sender_id_hash = "snd_v1_YWJjZGVmZ2hpamtsbW5vcA"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with audit_trace(trace_id), audit_sender(sender_id_hash):
        audit_event("test.event", message_count=1)
        audit_event("test.next", sender_known=True)

    events = _events(caplog)
    assert [event["trace_id"] for event in events] == [trace_id, trace_id]
    assert [event["sender_id_hash"] for event in events] == [
        sender_id_hash,
        sender_id_hash,
    ]


def test_audit_correlation_fields_sanitize_raw_sender_values(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    audit_event(
        "test.event",
        trace_id="not a safe trace id with spaces",
        sender_id_hash="alice.private@example.com",
    )

    event = _events(caplog)[0]
    assert event["trace_id"] == "unknown"
    assert event["sender_id_hash"] == "unknown"
    assert "alice.private@example.com" not in caplog.records[0].message


@pytest.mark.asyncio
async def test_register_person_audits_unauthenticated_return_path(caplog):
    from thenetwork.agent.tools import register_person

    ctx, _ = _tool_ctx(sender_authenticated=False)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    result = await register_person(ctx, name="Alice")

    assert result == {"status": "error", "reason": "sender_not_authenticated"}
    events = _events(caplog)
    database_event = _database_action_event(events, record_type="person")
    assert database_event["action"] == "insert"
    assert database_event["outcome"] == "rejected_unauthenticated"
    completed = _tool_completed_event(events, "register_person")
    assert completed["outcome"] == "success"
    assert completed["tool_outcome"] == "error"
    assert completed["tool_reason"] == "sender_not_authenticated"


@pytest.mark.asyncio
async def test_register_person_audits_already_registered_return_path(caplog):
    from thenetwork.agent.tools import register_person

    ctx, _ = _tool_ctx(sender_user_id="person-id")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    result = await register_person(ctx, name="Alice")

    assert result == {
        "status": "error",
        "reason": "already_registered",
        "person_id": "person-id",
    }
    events = _events(caplog)
    database_event = _database_action_event(events, record_type="person")
    assert database_event["action"] == "insert"
    assert database_event["outcome"] == "rejected_already_registered"
    completed = _tool_completed_event(events, "register_person")
    assert completed["tool_outcome"] == "error"
    assert completed["tool_reason"] == "already_registered"


@pytest.mark.asyncio
async def test_register_person_audits_existing_person_return_path(caplog):
    from thenetwork.agent.tools import register_person

    ctx, session = _tool_ctx()
    session.exec.return_value.first.return_value = SimpleNamespace(id="existing-id")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    result = await register_person(ctx, name="Alice")

    assert result == {"status": "exists", "person_id": "existing-id"}
    events = _events(caplog)
    database_event = _database_action_event(events, record_type="person")
    assert database_event["action"] == "lookup"
    assert database_event["outcome"] == "exists"
    completed = _tool_completed_event(events, "register_person")
    assert completed["tool_outcome"] == "exists"


@pytest.mark.asyncio
async def test_register_person_audits_rate_limited_return_path(caplog):
    from thenetwork.agent.tools import register_person

    ctx, session = _tool_ctx()
    ctx.deps.settings.registration_limit_per_day = 1
    session.exec.return_value.first.return_value = None
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with patch("thenetwork.agent.tools._hit_registration_quota", return_value=False):
        result = await register_person(ctx, name="Alice")

    assert result == {
        "status": "error",
        "reason": "registration_quota_exceeded",
        "limit": 1,
    }
    events = _events(caplog)
    database_event = _database_action_event(events, record_type="person")
    assert database_event["action"] == "insert"
    assert database_event["outcome"] == "rate_limited"
    completed = _tool_completed_event(events, "register_person")
    assert completed["tool_outcome"] == "error"
    assert completed["tool_reason"] == "registration_quota_exceeded"


@pytest.mark.asyncio
async def test_register_person_audits_created_return_path(caplog):
    from thenetwork.agent.tools import register_person

    ctx, session = _tool_ctx()
    session.exec.return_value.first.return_value = None
    session.refresh.side_effect = lambda person: setattr(person, "id", "new-person-id")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with patch("thenetwork.agent.tools._hit_registration_quota", return_value=True):
        result = await register_person(ctx, name="Alice")

    assert result == {"status": "created", "person_id": "new-person-id"}
    events = _events(caplog)
    database_event = _database_action_event(events, record_type="person")
    assert database_event["action"] == "insert"
    assert database_event["outcome"] == "success"
    completed = _tool_completed_event(events, "register_person")
    assert completed["tool_outcome"] == "created"


@pytest.mark.asyncio
async def test_propose_introduction_audits_run_proposal_cap_deferred(caplog):
    from thenetwork.agent.tools import propose_introduction

    ctx, _ = _tool_ctx(sender_user_id="alice-id")
    ctx.deps.settings.introduction_max_proposals_per_run = 1
    ctx.deps.introduction_proposal_count = 1
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    result = await propose_introduction(
        ctx, other_person_id="bob-id", sender_gist="a gist", other_gist="b gist"
    )

    assert result["status"] == "deferred"
    assert result["reason"] == "run_proposal_cap"
    assert result["limit"] == 1
    assert "no consent request was sent" in result["note"]
    events = _events(caplog)
    completed = _tool_completed_event(events, "propose_introduction")
    assert completed["tool_outcome"] == "deferred"
    assert completed["tool_reason"] == "run_proposal_cap"


@pytest.mark.asyncio
async def test_propose_introduction_audits_recipient_cap_deferred(caplog):
    from thenetwork.agent.tools import propose_introduction

    ctx, _ = _tool_ctx(sender_user_id="alice-id")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with patch(
        "thenetwork.agent.tools.propose_pair",
        return_value={
            "status": "deferred",
            "reason": "recipient_consent_request_cap",
            "limit": 3,
        },
    ):
        result = await propose_introduction(
            ctx, other_person_id="bob-id", sender_gist="a gist", other_gist="b gist"
        )

    assert result["status"] == "deferred"
    events = _events(caplog)
    completed = _tool_completed_event(events, "propose_introduction")
    assert completed["tool_outcome"] == "deferred"
    assert completed["tool_reason"] == "recipient_consent_request_cap"


@pytest.mark.asyncio
async def test_propose_introduction_audits_suppressed_consent_state(caplog):
    from thenetwork.agent.tools import propose_introduction

    ctx, _ = _tool_ctx(sender_user_id="alice-id")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with patch(
        "thenetwork.agent.tools.propose_pair",
        return_value={"status": "suppressed", "reason": "one_consented"},
    ):
        result = await propose_introduction(
            ctx, other_person_id="bob-id", sender_gist="a gist", other_gist="b gist"
        )

    assert result["status"] == "suppressed"
    events = _events(caplog)
    completed = _tool_completed_event(events, "propose_introduction")
    assert completed["tool_outcome"] == "suppressed"
    assert completed["tool_reason"] == "one_consented"


@pytest.mark.asyncio
async def test_agent_run_applies_configured_usage_limits():
    from thenetwork.agent.core import run_agent_for_email

    class FakeUsageLimits:
        def __init__(self, *, request_limit, total_tokens_limit):
            self.request_limit = request_limit
            self.total_tokens_limit = total_tokens_limit

    fake_result = SimpleNamespace(output="ok", all_messages=lambda: [])
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    settings = SimpleNamespace(
        agent_model="test:model",
        agent_request_limit=3,
        agent_total_tokens_limit=1234,
        admin_emails=[],
    )

    with (
        patch("thenetwork.agent.core.get_settings", return_value=settings),
        patch(
            "thenetwork.agent.core.build_agent", return_value=fake_agent
        ) as build_agent,
        patch(
            "thenetwork.agent.core.UsageLimits", side_effect=FakeUsageLimits
        ) as usage_limits,
    ):
        result = await run_agent_for_email(
            sender_email="alice.private@example.com",
            sender_user_id="opaque-person-id",
            email_subject="Hello",
            email_body="Please remember this",
        )

    assert result == "ok"
    build_agent.assert_called_once_with(model="test:model")
    usage_limits.assert_called_once_with(request_limit=3, total_tokens_limit=1234)
    fake_agent.run.assert_awaited_once()
    assert fake_agent.run.await_args.kwargs["usage_limits"].request_limit == 3
    assert fake_agent.run.await_args.kwargs["usage_limits"].total_tokens_limit == 1234


@pytest.mark.asyncio
async def test_agent_usage_limit_breach_is_audited_without_raising(caplog):
    from thenetwork.agent.core import run_agent_for_email

    class FakeUsageLimitExceeded(Exception):
        pass

    class FakeUsageLimits:
        def __init__(self, *, request_limit, total_tokens_limit):
            self.request_limit = request_limit
            self.total_tokens_limit = total_tokens_limit

    secrets = {
        "sender": "alice.private@example.com",
        "subject": "Confidential acquisition",
        "body": "Call me at 415-555-0100 about Project Finch",
    }
    fake_agent = SimpleNamespace(
        run=AsyncMock(side_effect=FakeUsageLimitExceeded("Project Finch token ceiling"))
    )
    settings = SimpleNamespace(
        agent_model="test:model",
        agent_request_limit=1,
        agent_total_tokens_limit=50,
        admin_emails=[],
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.get_settings", return_value=settings),
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.UsageLimits", side_effect=FakeUsageLimits),
        patch("thenetwork.agent.core.UsageLimitExceeded", FakeUsageLimitExceeded),
    ):
        result = await run_agent_for_email(
            sender_email=secrets["sender"],
            sender_user_id="opaque-person-id",
            email_subject=secrets["subject"],
            email_body=secrets["body"],
        )

    assert result == ""
    serialized = "\n".join(record.message for record in caplog.records)
    for secret in secrets.values():
        assert secret not in serialized
    assert "Project Finch token ceiling" not in serialized
    assert any(
        event["event"] == "agent.usage_limit_exceeded"
        and event["outcome"] == "error"
        and event["error_type"] == "FakeUsageLimitExceeded"
        for event in _events(caplog)
    )
    error_record = next(
        record
        for record in caplog.records
        if json.loads(record.message)["event"] == "agent.usage_limit_exceeded"
    )
    assert error_record.levelname == "ERROR"


@pytest.mark.asyncio
async def test_agent_usage_limit_breach_notifies_admins(caplog):
    from thenetwork.agent.core import run_agent_for_email

    class FakeUsageLimitExceeded(Exception):
        pass

    class FakeUsageLimits:
        def __init__(self, *, request_limit, total_tokens_limit):
            self.request_limit = request_limit
            self.total_tokens_limit = total_tokens_limit

    secrets = {
        "sender": "alice.private@example.com",
        "subject": "Confidential acquisition",
        "body": "Call me at 415-555-0100 about Project Finch",
    }
    fake_agent = SimpleNamespace(
        run=AsyncMock(side_effect=FakeUsageLimitExceeded("Project Finch token ceiling"))
    )
    settings = SimpleNamespace(
        agent_model="test:model",
        agent_request_limit=1,
        agent_total_tokens_limit=50,
        admin_emails=["admin@example.com"],
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.get_settings", return_value=settings),
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.UsageLimits", side_effect=FakeUsageLimits),
        patch("thenetwork.agent.core.UsageLimitExceeded", FakeUsageLimitExceeded),
        patch("thenetwork.agent.core.notify_admins") as mock_notify,
    ):
        result = await run_agent_for_email(
            sender_email=secrets["sender"],
            sender_user_id=None,
            email_subject=secrets["subject"],
            email_body=secrets["body"],
        )

    assert result == ""
    mock_notify.assert_called_once()
    call_args = mock_notify.call_args
    assert call_args.args[0] is settings
    notified_subject = call_args.args[1]
    notified_body = call_args.args[2]
    assert secrets["subject"] not in notified_subject
    assert secrets["subject"] not in notified_body
    assert secrets["body"] not in notified_body
    assert secrets["sender"] in notified_body
    assert "Sender known: False" in notified_body


@pytest.mark.asyncio
async def test_agent_trace_logs_structure_but_never_content(caplog):
    from thenetwork.agent.core import run_agent_for_email

    secrets = {
        "sender": "alice.private@example.com",
        "subject": "Confidential acquisition",
        "body": "Call me at 415-555-0100 about Project Finch",
        "reply": "I found Bob Smith for Project Finch",
        "thought": "Alice should meet Bob Smith",
        "tool_args": '{"query":"Bob Smith alice.private@example.com"}',
    }
    fake_result = SimpleNamespace(
        output=secrets["reply"],
        all_messages=lambda: [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(part_kind="thinking", content=secrets["thought"]),
                    SimpleNamespace(
                        part_kind="tool-call",
                        tool_name="search",
                        args=secrets["tool_args"],
                    ),
                ]
            )
        ],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins"),
    ):
        result = await run_agent_for_email(
            sender_email=secrets["sender"],
            sender_user_id="opaque-person-id",
            email_subject=secrets["subject"],
            email_body=secrets["body"],
        )

    assert result == secrets["reply"]
    serialized = "\n".join(record.message for record in caplog.records)
    for secret in secrets.values():
        assert secret not in serialized

    events = _events(caplog)
    event_names = {event["event"] for event in events}
    assert {
        "agent.run.started",
        "agent.prompt_constructed",
        "agent.model_trace",
        "agent.response_generated",
        "agent.run.completed",
    } <= event_names
    trace = next(event for event in events if event["event"] == "agent.model_trace")
    assert trace["part_kinds"] == ["thinking", "tool-call"]
    assert trace["tool_names"] == ["search"]


def test_model_response_audit_logs_redacted_complete_parts(caplog, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart

    from thenetwork import audit

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    class Analyzer:
        def analyze(self, *, text, language):
            matches = []
            for value, entity_type in (
                ("Alice Example", "PERSON"),
                ("alice@example.test", "EMAIL_ADDRESS"),
            ):
                start = text.find(value)
                if start >= 0:
                    matches.append(
                        SimpleNamespace(
                            start=start,
                            end=start + len(value),
                            entity_type=entity_type,
                        )
                    )
            return matches

    monkeypatch.setattr(
        "thenetwork.security.log_redaction._get_log_analyzer",
        Analyzer,
    )
    raw_name = "Alice Example"
    raw_email = "alice@example.test"
    result = SimpleNamespace(
        all_messages=lambda: [
            ModelResponse(
                parts=[
                    TextPart(content=f"Reply to {raw_name}"),
                    ThinkingPart(content=f"Contact {raw_email}"),
                    ToolCallPart(
                        tool_name="search",
                        args={"query": f"{raw_name} https://example.test/path"},
                    ),
                ]
            )
        ]
    )

    audit.audit_model_trace(result, pseudonym_secret="test-key")

    serialized = "\n".join(record.message for record in caplog.records)
    assert raw_name not in serialized
    assert raw_email not in serialized
    assert "example.test" not in serialized
    response = next(
        event for event in _events(caplog) if event["event"] == "agent.model_response"
    )
    parts = response["response"]["parts"]
    assert [part["part_kind"] for part in parts] == ["text", "thinking", "tool-call"]


@pytest.mark.asyncio
async def test_agent_run_audits_trace_id_on_lifecycle_events(caplog):
    from thenetwork.agent.core import run_agent_for_email
    from thenetwork.security.sender_identifier import sender_identifier

    trace_id = "8b8c9907-2d7b-4347-967a-412c6fe63c27"
    sender_email = "alice.private@example.com"
    expected_sender_id_hash = sender_identifier(sender_email, secret="audit-secret")
    fake_result = SimpleNamespace(output="ok", all_messages=lambda: [])
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins"),
        patch(
            "thenetwork.security.sender_identifier.get_settings",
            return_value=SimpleNamespace(sender_identifier_secret="audit-secret"),
        ),
    ):
        result = await run_agent_for_email(
            sender_email=sender_email,
            sender_user_id="opaque-person-id",
            email_subject="Hello",
            email_body="Please remember this",
            trace_id=trace_id,
        )

    assert result == "ok"
    lifecycle = [
        event for event in _events(caplog) if event["event"].startswith("agent.")
    ]
    assert lifecycle
    assert {event["trace_id"] for event in lifecycle} == {trace_id}
    assert {event["sender_id_hash"] for event in lifecycle} == {
        expected_sender_id_hash,
    }
    assert sender_email not in "\n".join(record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_agent_no_tool_call_is_flagged_without_admin_notification(caplog):
    """A validator-bypassing double is audited but cannot create an alert."""
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(
        output="I don't know where to get a pizza.",
        all_messages=lambda: [
            SimpleNamespace(
                parts=[SimpleNamespace(part_kind="thinking", content="no tool fits")]
            ),
            SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        part_kind="text", content="I don't know where to get a pizza."
                    )
                ]
            ),
        ],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        await run_agent_for_email(
            sender_email="mike@mkly.io",
            sender_user_id=None,
            email_subject="I'm hungry",
            email_body="I would really like a pizza.",
            trace_id="8b8c9907-2d7b-4347-967a-412c6fe63c27",
        )

    events = _events(caplog)
    response_event = next(e for e in events if e["event"] == "agent.response_generated")
    assert response_event["tool_called"] is False
    no_action_events = [e for e in events if e["event"] == "agent.no_action_taken"]
    assert len(no_action_events) == 1
    assert no_action_events[0]["sender_known"] is False
    undispatched = next(
        e for e in events if e["event"] == "agent.undispatched_response"
    )
    assert undispatched["trace_id"] == "8b8c9907-2d7b-4347-967a-412c6fe63c27"
    notify_admins.assert_not_called()
    assert "pizza" not in "\n".join(record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_empty_agent_output_does_not_escalate_as_undispatched():
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(output="   ", all_messages=lambda: [])
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        await run_agent_for_email(
            sender_email="mike@mkly.io",
            sender_user_id=None,
            email_subject="No action",
            email_body="FYI",
        )

    notify_admins.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_no_action_is_audited_without_admin_notification(caplog):
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(
        output="No specific common ground supports an introduction.",
        all_messages=lambda: [],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        await run_agent_for_email(
            sender_email="mike@mkly.io",
            sender_user_id="person-mike",
            email_subject="[Proactive] Possible connection",
            email_body="[System match] Consider a connection.",
            is_proactive=True,
        )

    events = _events(caplog)
    assert any(event["event"] == "agent.proactive_no_action" for event in events)
    assert not any(event["event"] == "agent.undispatched_response" for event in events)
    notify_admins.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_no_op_alert_regression_fixture_has_no_admin_messages(caplog):
    """Seventeen no-op proactive jobs model the alert-heavy simulation run."""
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(
        output="No supported action for this proactive candidate.",
        all_messages=lambda: [],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        for _ in range(17):
            await run_agent_for_email(
                sender_email="mike@mkly.io",
                sender_user_id="person-mike",
                email_subject="[Proactive] Possible connection",
                email_body="[System match] Consider a connection.",
                is_proactive=True,
            )

    assert (
        len([e for e in _events(caplog) if e["event"] == "agent.proactive_no_action"])
        == 17
    )
    notify_admins.assert_not_called()


@pytest.mark.asyncio
async def test_server_side_send_prevents_undispatched_escalation():
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(
        output="A consent request was sent.", all_messages=lambda: []
    )

    async def run_with_server_side_send(_message, *, deps, usage_limits):
        deps.server_side_send_count = 2
        return fake_result

    fake_agent = SimpleNamespace(run=AsyncMock(side_effect=run_with_server_side_send))

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        await run_agent_for_email(
            sender_email="mike@mkly.io",
            sender_user_id="person-mike",
            email_subject="Introduce me",
            email_body="Please make the introduction.",
        )

    notify_admins.assert_not_called()


@pytest.mark.asyncio
async def test_reply_tool_call_prevents_undispatched_escalation():
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(
        output="Your reply was sent.",
        all_messages=lambda: [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        part_kind="tool-call",
                        tool_name="reply_to_sender",
                    )
                ]
            )
        ],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        await run_agent_for_email(
            sender_email="mike@mkly.io",
            sender_user_id="person-mike",
            email_subject="Reply",
            email_body="Please respond.",
        )

    notify_admins.assert_not_called()


@pytest.mark.asyncio
async def test_no_action_tool_call_prevents_undispatched_escalation():
    from thenetwork.agent.core import run_agent_for_email

    fake_result = SimpleNamespace(
        output="No action was needed.",
        all_messages=lambda: [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        part_kind="tool-call",
                        tool_name="no_action",
                    )
                ]
            )
        ],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))

    with (
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
        patch("thenetwork.agent.core.notify_admins") as notify_admins,
    ):
        await run_agent_for_email(
            sender_email="mike@mkly.io",
            sender_user_id="person-mike",
            email_subject="Thanks",
            email_body="No new info here.",
        )

    notify_admins.assert_not_called()


@pytest.mark.asyncio
async def test_tool_and_database_events_do_not_log_arguments(caplog):
    from thenetwork.agent.deps import AgentDeps
    from thenetwork.agent.tools import search
    from thenetwork.search.match import MemoryMatch

    secret_query = "Find Alice at alice.private@example.com"
    secret_gist = "Alice is quietly raising a seed round"
    ctx = SimpleNamespace(deps=AgentDeps())
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    matches = [
        MemoryMatch(
            memory_id="memory-id",
            person_id="opaque-id",
            gist=secret_gist,
            similarity=0.9,
        )
    ]
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.agent.tools._get_session", return_value=session),
        patch(
            "thenetwork.agent.tools.embed_text",
            new_callable=AsyncMock,
            return_value=[0.0] * 1536,
        ),
        patch("thenetwork.agent.tools.match_memories", return_value=matches),
    ):
        result = await search(ctx, query=secret_query)

    assert result[0]["evidence"] == [{"gist": secret_gist}]
    serialized = "\n".join(record.message for record in caplog.records)
    assert secret_query not in serialized
    assert secret_gist not in serialized
    events = _events(caplog)
    assert any(event["event"] == "agent.tool.completed" for event in events)
    assert any(
        event["event"] == "database.action" and event["result_count"] == 1
        for event in events
    )


def test_intake_logs_header_metadata_without_values(caplog):
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.worker.producer import _poll_and_enqueue

    message = InboundMessage(
        uid="123",
        sender="alice.private@example.com",
        subject="Confidential acquisition",
        body="Project Finch closes Friday",
        auto_submitted=None,
        sender_authenticated=True,
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_and_enqueue() == 1

    process_email.defer.assert_called_once_with(
        sender_email=message.sender,
        subject=message.subject,
        body=message.body,
        sender_authenticated=message.sender_authenticated,
        sender_display_name=message.sender_display_name,
        raw_message_b64=None,
        trace_id=message.trace_id,
        source_mailbox="primary",
    )
    mark_seen.assert_called_once_with(["123"], mailbox="primary")
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    assert message.body not in serialized
    received = next(
        event
        for event in _events(caplog)
        if event["event"] == "intake.message_received"
    )
    assert received["header_names"] == ["from", "subject", "auto-submitted"]
    assert received["trace_id"] == message.trace_id


def test_intake_enqueues_inbound_message_id_when_present(caplog):
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.worker.producer import _poll_and_enqueue

    message = InboundMessage(
        uid="123",
        sender="alice.private@example.com",
        subject="Confidential acquisition",
        body="Project Finch closes Friday",
        auto_submitted=None,
        sender_authenticated=True,
        message_id="<abc123@example.com>",
        message_references="<root@example.com> <parent@example.com>",
        message_date="Sat, 04 Jul 2026 12:00:00 -0700",
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen"),
        patch("thenetwork.worker.producer.is_message_processed", return_value=False),
        patch("thenetwork.worker.producer.mark_message_processed") as mark_processed,
    ):
        assert _poll_and_enqueue() == 1

    process_email.defer.assert_called_once_with(
        sender_email=message.sender,
        subject=message.subject,
        body=message.body,
        sender_authenticated=message.sender_authenticated,
        sender_display_name=message.sender_display_name,
        raw_message_b64=None,
        trace_id=message.trace_id,
        source_mailbox="primary",
        inbound_message_id=message.message_id,
        inbound_references=message.message_references,
        inbound_body_for_quote=message.body,
        inbound_date=message.message_date,
    )
    mark_processed.assert_called_once_with(message.message_id)


def test_intake_enqueues_recipient_without_audit_logging_it(caplog):
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.worker.producer import _poll_and_enqueue

    recipient = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.private.example"
    message = InboundMessage(
        uid="123",
        sender="alice.private@example.com",
        subject="Relay reply",
        body="Private reply body",
        auto_submitted=None,
        sender_authenticated=True,
        recipient_address=recipient,
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen"),
    ):
        assert _poll_and_enqueue() == 1

    assert process_email.defer.call_args.kwargs["recipient_address"] == recipient
    assert recipient not in "\n".join(record.message for record in caplog.records)


def test_intake_reserves_message_id_before_defer_and_releases_on_defer_failure():
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.worker.producer import _poll_and_enqueue

    message = InboundMessage(
        uid="123",
        sender="alice@example.com",
        subject="Hello",
        body="Need an intro",
        auto_submitted=None,
        sender_authenticated=True,
        message_id="<abc@example.com>",
    )
    calls: list[str] = []
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.is_message_processed", return_value=False),
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
        patch(
            "thenetwork.worker.producer.mark_message_processed",
            side_effect=lambda _: calls.append("reserve"),
        ),
        patch(
            "thenetwork.worker.producer.unmark_message_processed",
            side_effect=lambda _: calls.append("release"),
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
    ):
        process_email.defer.side_effect = RuntimeError("queue unavailable")
        with pytest.raises(RuntimeError, match="queue unavailable"):
            _poll_and_enqueue()

    assert calls == ["reserve", "release"]
    mark_seen.assert_not_called()


def test_intake_enqueue_audits_and_defers_same_trace_id(caplog):
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.security.sender_identifier import sender_identifier
    from thenetwork.worker.producer import _poll_and_enqueue

    message = InboundMessage(
        uid="123",
        sender="alice.private@example.com",
        subject="Confidential acquisition",
        body="Project Finch closes Friday",
        auto_submitted=None,
        sender_authenticated=True,
        trace_id="399005c4-1494-4c94-bc5c-cc1036666679",
    )
    expected_sender_id_hash = sender_identifier(message.sender, secret="audit-secret")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen"),
        patch(
            "thenetwork.security.sender_identifier.get_settings",
            return_value=SimpleNamespace(sender_identifier_secret="audit-secret"),
        ),
    ):
        assert _poll_and_enqueue() == 1

    process_email.defer.assert_called_once()
    assert process_email.defer.call_args.kwargs["trace_id"] == message.trace_id
    received = next(
        event
        for event in _events(caplog)
        if event["event"] == "intake.message_received"
    )
    assert received["trace_id"] == message.trace_id
    assert received["sender_id_hash"] == expected_sender_id_hash
    assert message.sender not in "\n".join(record.message for record in caplog.records)


def test_intake_skips_duplicate_message_id_without_reenqueueing(caplog):
    """If a Message-ID was already processed (e.g. \\Seen got reset after the
    fact), the producer must not re-enqueue it - that would re-run the agent
    against an already-handled email, risking duplicate replies/memories/sends."""
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.worker.producer import _poll_and_enqueue

    message = InboundMessage(
        uid="123",
        sender="alice.private@example.com",
        subject="Confidential acquisition",
        body="Project Finch closes Friday",
        auto_submitted=None,
        sender_authenticated=True,
        message_id="<abc123@example.com>",
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
        patch("thenetwork.worker.producer.is_message_processed", return_value=True),
        patch("thenetwork.worker.producer.mark_message_processed") as mark_processed,
    ):
        assert _poll_and_enqueue() == 0

    process_email.defer.assert_not_called()
    mark_processed.assert_not_called()
    mark_seen.assert_called_once_with(["123"], mailbox="primary")
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    duplicate = next(
        event
        for event in _events(caplog)
        if event["event"] == "intake.message_duplicate_skipped"
    )
    assert duplicate["subject_chars"] == len(message.subject)


def test_intake_rejects_bad_shape_without_enqueueing_or_replying(caplog):
    from thenetwork.email.inbound import REJECT_BODY_OVERSIZE, InboundMessage
    from thenetwork.worker.producer import _poll_and_enqueue

    message = InboundMessage(
        uid="123",
        sender="alice.private@example.com",
        subject="Confidential acquisition",
        body="",
        auto_submitted=None,
        sender_authenticated=True,
        rejection_reason=REJECT_BODY_OVERSIZE,
        body_chars=100_001,
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_and_enqueue() == 0

    process_email.defer.assert_not_called()
    mark_seen.assert_called_once_with(["123"], mailbox="primary")
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    rejected = next(
        event
        for event in _events(caplog)
        if event["event"] == "intake.message_rejected"
    )
    assert rejected["reason"] == REJECT_BODY_OVERSIZE
    assert rejected["body_chars"] == 100_001


def test_intake_disposable_rejection_audit_is_pii_safe(caplog):
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.worker.producer import (
        REJECT_DISPOSABLE_DOMAIN,
        _poll_and_enqueue,
    )

    message = InboundMessage(
        uid="123",
        sender="private-user@mailinator.com",
        subject="Confidential acquisition",
        body="Project Finch closes Friday",
        auto_submitted=None,
        sender_authenticated=True,
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_and_enqueue() == 0

    process_email.defer.assert_not_called()
    mark_seen.assert_called_once_with(["123"], mailbox="primary")
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    assert message.body not in serialized
    rejected = next(
        event
        for event in _events(caplog)
        if event["event"] == "intake.message_rejected"
    )
    assert rejected["reason"] == REJECT_DISPOSABLE_DOMAIN


def test_new_sender_burst_audit_contains_only_bounded_metadata(caplog):
    from thenetwork.email.inbound import InboundMessage
    from thenetwork.email.intake_observations import BurstObservationResult
    from thenetwork.worker.producer import _poll_mailbox_and_enqueue

    message = InboundMessage(
        uid="burst-1",
        sender="private.sender@example.com",
        subject="Private campaign subject",
        body="Private campaign body",
        auto_submitted=None,
        sender_authenticated=False,
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.get_settings",
            return_value=SimpleNamespace(
                primary_intake_burst_monitoring_enabled=True,
                sender_identifier_secret="monitor-secret",
                relay_domain="relay.example.com",
            ),
        ),
        patch(
            "thenetwork.worker.producer.observe_primary_intake_batch",
            return_value=BurstObservationResult(
                paused=True,
                newly_observed=25,
                distinct_new_senders=25,
            ),
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

    process_email.defer.assert_not_called()
    mark_seen.assert_called_once_with([], mailbox="primary")
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    assert message.body not in serialized
    event = next(
        event
        for event in _events(caplog)
        if event["event"] == "intake.new_sender_burst_detected"
    )
    assert event["reason"] == "new_sender_burst"
    assert event["message_count"] == 25
    assert event["result_count"] == 25


@pytest.mark.asyncio
async def test_worker_rejection_logs_reason_without_message_content(caplog):
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with (
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=False),
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup(None),
        ),
    ):
        await process_email.func(
            sender_email="alice.private@example.com",
            subject="Confidential acquisition",
            body="Project Finch closes Friday",
        )

    serialized = "\n".join(record.message for record in caplog.records)
    assert "alice.private@example.com" not in serialized
    assert "Confidential acquisition" not in serialized
    assert "Project Finch closes Friday" not in serialized
    assert any(
        event["event"] == "worker.message_rejected" and event["reason"] == "rate_limit"
        for event in _events(caplog)
    )


@pytest.mark.asyncio
async def test_agent_failure_is_audited_and_reraised_for_retry(caplog):
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with (
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup("user-alice"),
        ),
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            AsyncMock(side_effect=RuntimeError("provider unavailable")),
        ),
        patch("thenetwork.worker.tasks.notify_admins") as notify,
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await process_email.func(
                sender_email="alice@example.com",
                subject="Hi",
                body="Please introduce me",
                sender_authenticated=True,
            )

    notify.assert_not_called()
    assert any(
        event["event"] == "worker.agent_failed"
        and event["error_type"] == "RuntimeError"
        for event in _events(caplog)
    )


@pytest.mark.asyncio
async def test_agent_failure_notifies_admins_on_final_retry_only():
    from thenetwork.worker.tasks import process_email

    final_context = SimpleNamespace(job=SimpleNamespace(attempts=3))
    with (
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup("user-alice"),
        ),
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            AsyncMock(side_effect=RuntimeError()),
        ),
        patch("thenetwork.worker.tasks.notify_admins") as notify,
    ):
        with pytest.raises(RuntimeError):
            await process_email.func(
                final_context,
                sender_email="alice@example.com",
                subject="Hi",
                body="Please introduce me",
                sender_authenticated=True,
            )

    notify.assert_called_once()


@pytest.mark.asyncio
async def test_worker_caps_subject_and_body_before_agent():
    from thenetwork.email.inbound import MAX_BODY_CHARS, MAX_SUBJECT_CHARS
    from thenetwork.worker.tasks import process_email

    mock_agent = AsyncMock()

    with (
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ) as scan_content,
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch("thenetwork.worker.tasks.get_session") as mock_get_session,
        patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent),
    ):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = None
        mock_session.exec.return_value.first.return_value = None
        mock_get_session.return_value = mock_session
        await process_email.func(
            sender_email="alice@example.com",
            subject="s" * (MAX_SUBJECT_CHARS + 20),
            body="b" * (MAX_BODY_CHARS + 20),
            sender_authenticated=True,
        )

    scan_content.assert_awaited_once_with("b" * MAX_BODY_CHARS)
    mock_agent.assert_awaited_once()
    _, kwargs = mock_agent.await_args
    assert kwargs["email_subject"] == "s" * MAX_SUBJECT_CHARS
    assert kwargs["email_body"] == "b" * MAX_BODY_CHARS


@pytest.mark.asyncio
async def test_worker_threads_trace_id_to_agent_and_audit(caplog):
    from thenetwork.security.sender_identifier import sender_identifier
    from thenetwork.worker.tasks import process_email

    trace_id = "9f97d361-4ccb-4638-a0bf-98bdbfd254b1"
    sender_email = "alice@example.com"
    expected_sender_id_hash = sender_identifier(sender_email, secret="audit-secret")
    mock_agent = AsyncMock()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup("person-id"),
        ),
        patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent),
        patch(
            "thenetwork.security.sender_identifier.get_settings",
            return_value=SimpleNamespace(sender_identifier_secret="audit-secret"),
        ),
    ):
        await process_email.func(
            sender_email=sender_email,
            subject="Hello",
            body="Project Finch closes Friday",
            sender_authenticated=True,
            trace_id=trace_id,
        )

    mock_agent.assert_awaited_once()
    assert mock_agent.await_args.kwargs["trace_id"] == trace_id
    worker_events = [
        event
        for event in _events(caplog)
        if event["event"].startswith("worker.") or event["event"] == "database.action"
    ]
    assert worker_events
    assert {event["trace_id"] for event in worker_events} == {trace_id}
    assert {event["sender_id_hash"] for event in worker_events} == {
        expected_sender_id_hash,
    }


@pytest.mark.asyncio
async def test_worker_rejects_oversized_body_without_reply_or_agent(caplog):
    from thenetwork.email.inbound import MAX_RAW_BODY_CHARS, REJECT_BODY_OVERSIZE
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with (
        patch("thenetwork.worker.tasks.check_rate_limit") as check_rate_limit,
        patch("thenetwork.worker.tasks.scan_content") as scan_content,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup(None),
        ),
    ):
        await process_email.func(
            sender_email="alice@example.com",
            subject="Hello",
            body="a" * (MAX_RAW_BODY_CHARS + 1),
        )

    check_rate_limit.assert_not_called()
    scan_content.assert_not_called()
    send_reply.assert_not_called()
    mock_agent.assert_not_called()
    assert any(
        event["event"] == "worker.message_rejected"
        and event["reason"] == REJECT_BODY_OVERSIZE
        for event in _events(caplog)
    )


@pytest.mark.parametrize("reason", ["body_oversize", "rate_limit", "content_scan"])
@pytest.mark.asyncio
async def test_worker_replies_to_known_authenticated_sender_on_infrastructure_rejection(
    reason,
):
    from thenetwork.email.inbound import MAX_RAW_BODY_CHARS, REJECT_BODY_OVERSIZE
    from thenetwork.worker.tasks import (
        REJECT_CONTENT_SCAN,
        REJECT_RATE_LIMIT,
        process_email,
    )
    from thenetwork.email.render import (
        FixedEmailTemplate,
        InfrastructureRejectionEmailContext,
        InfrastructureRejectionReason,
    )

    body = "Project Finch closes Friday"
    if reason == REJECT_BODY_OVERSIZE:
        body = "a" * (MAX_RAW_BODY_CHARS + 1)

    mock_session = _mock_sender_lookup("person-id")

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch(
            "thenetwork.worker.tasks.check_rate_limit",
            return_value=reason != REJECT_RATE_LIMIT,
        ) as check_rate_limit,
        patch(
            "thenetwork.worker.tasks.scan_content",
            return_value=(reason != REJECT_CONTENT_SCAN, None),
        ) as scan_content,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="alice@example.com",
            subject="Hello",
            body=body,
            sender_authenticated=True,
        )

    send_reply.assert_called_once_with(
        to_address="alice@example.com",
        subject="Re: Hello",
        fixed_template=FixedEmailTemplate.INFRASTRUCTURE_REJECTION,
        fixed_context=InfrastructureRejectionEmailContext(
            InfrastructureRejectionReason(reason)
        ),
    )
    mock_agent.assert_not_called()

    if reason == REJECT_BODY_OVERSIZE:
        check_rate_limit.assert_not_called()
        scan_content.assert_not_called()


@pytest.mark.asyncio
async def test_worker_threads_infrastructure_rejection_reply():
    from thenetwork.worker.tasks import process_email
    from thenetwork.email.render import (
        FixedEmailTemplate,
        InfrastructureRejectionEmailContext,
        InfrastructureRejectionReason,
    )

    mock_session = _mock_sender_lookup("person-id")

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=False),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()),
    ):
        await process_email.func(
            sender_email="alice@example.com",
            subject="Hello",
            body="Project Finch closes Friday",
            sender_authenticated=True,
            inbound_message_id="<abc123@example.com>",
            inbound_references="<root@example.com> <parent@example.com>",
            inbound_body_for_quote="Project Finch closes Friday",
            inbound_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    send_reply.assert_called_once_with(
        to_address="alice@example.com",
        subject="Re: Hello",
        fixed_template=FixedEmailTemplate.INFRASTRUCTURE_REJECTION,
        fixed_context=InfrastructureRejectionEmailContext(
            InfrastructureRejectionReason.RATE_LIMIT
        ),
        in_reply_to="<abc123@example.com>",
        references="<root@example.com> <parent@example.com> <abc123@example.com>",
        quoted_body_text="Project Finch closes Friday",
        quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
    )


@pytest.mark.asyncio
async def test_worker_sends_verified_admin_command_reply_as_internal_plain_mail():
    from thenetwork.worker.tasks import process_email

    with (
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup(None),
        ),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "thenetwork.worker.tasks.verify_admin_request",
            return_value="COMMAND: status",
        ),
        patch("thenetwork.worker.tasks.extract_command", return_value="status"),
        patch("thenetwork.worker.tasks.extract_body_text", return_value=""),
        patch(
            "thenetwork.worker.tasks.handle_admin_command",
            new=AsyncMock(return_value="admin result"),
        ),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
    ):
        await process_email.func(
            sender_email="admin@example.com",
            subject="ADMIN: status",
            body="signed command",
            raw_message_b64="c2lnbmVk",
        )

    send_reply.assert_called_once_with(
        to_address="admin@example.com",
        subject="Re: ADMIN: status",
        body_text="admin result",
        audience="internal",
    )


@pytest.mark.parametrize("reason", ["body_oversize", "rate_limit", "content_scan"])
@pytest.mark.parametrize(
    ("sender_authenticated", "sender_id"),
    [(False, "person-id"), (True, None)],
)
@pytest.mark.asyncio
async def test_worker_keeps_infrastructure_rejection_silent_for_unauthenticated_or_unknown_sender(
    reason,
    sender_authenticated,
    sender_id,
):
    from thenetwork.email.inbound import MAX_RAW_BODY_CHARS, REJECT_BODY_OVERSIZE
    from thenetwork.worker.tasks import (
        REJECT_CONTENT_SCAN,
        REJECT_RATE_LIMIT,
        process_email,
    )

    body = "Project Finch closes Friday"
    if reason == REJECT_BODY_OVERSIZE:
        body = "a" * (MAX_RAW_BODY_CHARS + 1)

    mock_session = _mock_sender_lookup(sender_id)

    with (
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=mock_session,
        ) as get_session,
        patch(
            "thenetwork.worker.tasks.check_rate_limit",
            return_value=reason != REJECT_RATE_LIMIT,
        ),
        patch(
            "thenetwork.worker.tasks.scan_content",
            return_value=(reason != REJECT_CONTENT_SCAN, None),
        ),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="alice@example.com",
            subject="Hello",
            body=body,
            sender_authenticated=sender_authenticated,
        )

    send_reply.assert_not_called()
    mock_agent.assert_not_called()
    if sender_authenticated:
        assert get_session.call_count == 2
    else:
        get_session.assert_called_once()


@pytest.mark.asyncio
async def test_worker_skips_empty_body_after_rate_limit_without_agent(caplog):
    from thenetwork.email.inbound import REJECT_BODY_EMPTY
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with (
        patch(
            "thenetwork.worker.tasks.check_rate_limit", return_value=True
        ) as check_rate_limit,
        patch("thenetwork.worker.tasks.scan_content") as scan_content,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
        patch(
            "thenetwork.worker.tasks.get_session",
            return_value=_mock_sender_lookup(None),
        ),
    ):
        await process_email.func(
            sender_email="alice@example.com",
            subject="Hello",
            body=" \n",
        )

    check_rate_limit.assert_called_once_with(
        "alice@example.com",
        sender_authenticated=False,
    )
    scan_content.assert_not_called()
    send_reply.assert_not_called()
    mock_agent.assert_not_called()
    assert any(
        event["event"] == "worker.message_rejected"
        and event["reason"] == REJECT_BODY_EMPTY
        for event in _events(caplog)
    )
