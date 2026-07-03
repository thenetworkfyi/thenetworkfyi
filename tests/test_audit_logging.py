"""Structured audit coverage without creating a PII-at-rest side channel."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.audit import LOGGER_NAME, audit_event, audit_run


def _events(caplog) -> list[dict]:
    return [json.loads(record.message) for record in caplog.records if record.name == LOGGER_NAME]


def test_audit_events_are_json_and_correlated(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with audit_run():
        audit_event("test.event", message_count=2, outcome="success")
        audit_event("test.next", sender_known=True)

    events = _events(caplog)
    assert [event["event"] for event in events] == ["test.event", "test.next"]
    assert events[0]["run_id"] == events[1]["run_id"]
    assert events[0]["timestamp"].endswith("+00:00")


def test_audit_api_rejects_content_bearing_fields():
    with pytest.raises(ValueError, match="unsafe audit fields"):
        audit_event("unsafe", email="person@example.com", body="private")


def test_audit_categories_replace_arbitrary_values(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    audit_event("worker.message_rejected", reason="private_secret")

    assert _events(caplog)[0]["reason"] == "unknown"
    assert "private_secret" not in caplog.records[0].message


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
    )

    with patch("thenetwork.agent.core.get_settings", return_value=settings), \
         patch("thenetwork.agent.core.build_agent", return_value=fake_agent) as build_agent, \
         patch("thenetwork.agent.core.UsageLimits", side_effect=FakeUsageLimits) as usage_limits:
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
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with patch("thenetwork.agent.core.get_settings", return_value=settings), \
         patch("thenetwork.agent.core.build_agent", return_value=fake_agent), \
         patch("thenetwork.agent.core.UsageLimits", side_effect=FakeUsageLimits), \
         patch("thenetwork.agent.core.UsageLimitExceeded", FakeUsageLimitExceeded):
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
                    SimpleNamespace(part_kind="tool-call", args=secrets["tool_args"]),
                ]
            )
        ],
    )
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with patch("thenetwork.agent.core.build_agent", return_value=fake_agent):
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

    with patch("thenetwork.agent.tools._get_session", return_value=session), patch(
        "thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536
    ), patch("thenetwork.agent.tools.match_memories", return_value=matches):
        result = await search(ctx, query=secret_query)

    assert result[0]["gist"] == secret_gist
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

    with patch("thenetwork.worker.producer.poll_unseen", return_value=[message]), patch(
        "thenetwork.worker.producer.process_email"
    ) as process_email, patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen:
        assert _poll_and_enqueue() == 1

    process_email.defer.assert_called_once_with(
        sender_email=message.sender,
        subject=message.subject,
        body=message.body,
        sender_authenticated=message.sender_authenticated,
    )
    mark_seen.assert_called_once_with(["123"])
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    assert message.body not in serialized
    received = next(event for event in _events(caplog) if event["event"] == "intake.message_received")
    assert received["header_names"] == ["from", "subject", "auto-submitted"]


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

    with patch("thenetwork.worker.producer.poll_unseen", return_value=[message]), patch(
        "thenetwork.worker.producer.process_email"
    ) as process_email, patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen:
        assert _poll_and_enqueue() == 0

    process_email.defer.assert_not_called()
    mark_seen.assert_called_once_with(["123"])
    serialized = "\n".join(record.message for record in caplog.records)
    assert message.sender not in serialized
    assert message.subject not in serialized
    rejected = next(event for event in _events(caplog) if event["event"] == "intake.message_rejected")
    assert rejected["reason"] == REJECT_BODY_OVERSIZE
    assert rejected["body_chars"] == 100_001


@pytest.mark.asyncio
async def test_worker_rejection_logs_reason_without_message_content(caplog):
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=False):
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
async def test_worker_caps_subject_and_body_before_agent():
    from thenetwork.email.inbound import MAX_BODY_CHARS, MAX_SUBJECT_CHARS
    from thenetwork.worker.tasks import process_email

    mock_agent = AsyncMock()

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), patch(
        "thenetwork.worker.tasks.scan_content", return_value=(True, None)
    ) as scan_content, patch("thenetwork.worker.tasks.is_admin_request", return_value=False), patch(
        "thenetwork.worker.tasks.run_agent_for_email", mock_agent
    ):
        await process_email.func(
            sender_email="alice@example.com",
            subject="s" * (MAX_SUBJECT_CHARS + 20),
            body="b" * (MAX_BODY_CHARS + 20),
        )

    scan_content.assert_called_once_with("b" * MAX_BODY_CHARS)
    mock_agent.assert_awaited_once()
    _, kwargs = mock_agent.await_args
    assert kwargs["email_subject"] == "s" * MAX_SUBJECT_CHARS
    assert kwargs["email_body"] == "b" * MAX_BODY_CHARS


@pytest.mark.asyncio
async def test_worker_rejects_oversized_body_without_reply_or_agent(caplog):
    from thenetwork.email.inbound import MAX_RAW_BODY_CHARS, REJECT_BODY_OVERSIZE
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with patch("thenetwork.worker.tasks.check_rate_limit") as check_rate_limit, patch(
        "thenetwork.worker.tasks.scan_content"
    ) as scan_content, patch("thenetwork.worker.tasks.send_reply") as send_reply, patch(
        "thenetwork.worker.tasks.run_agent_for_email", AsyncMock()
    ) as mock_agent:
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
        event["event"] == "worker.message_rejected" and event["reason"] == REJECT_BODY_OVERSIZE
        for event in _events(caplog)
    )


@pytest.mark.asyncio
async def test_worker_skips_empty_body_before_reply_or_agent(caplog):
    from thenetwork.email.inbound import REJECT_BODY_EMPTY
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with patch("thenetwork.worker.tasks.check_rate_limit") as check_rate_limit, patch(
        "thenetwork.worker.tasks.scan_content"
    ) as scan_content, patch("thenetwork.worker.tasks.send_reply") as send_reply, patch(
        "thenetwork.worker.tasks.run_agent_for_email", AsyncMock()
    ) as mock_agent:
        await process_email.func(
            sender_email="alice@example.com",
            subject="Hello",
            body=" \n",
        )

    check_rate_limit.assert_not_called()
    scan_content.assert_not_called()
    send_reply.assert_not_called()
    mock_agent.assert_not_called()
    assert any(
        event["event"] == "worker.message_rejected" and event["reason"] == REJECT_BODY_EMPTY
        for event in _events(caplog)
    )
