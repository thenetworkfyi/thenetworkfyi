from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentDeps
from thenetwork.audit import LOGGER_NAME
from thenetwork.db.models import Event, Person
from thenetwork.settings import Settings


@pytest.mark.asyncio
async def test_model_retry_replays_mutating_tools_without_repeating_effects(caplog):
    """A retry may repeat completed calls without duplicating their effects."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    expires_at = "2099-07-18T20:00:00+00:00"
    first_event_args = {
        "text": "A recurring bakery logistics meetup",
        "expires_at": expires_at,
        "recurrence": "monthly",
    }
    distinct_event_args = {
        "text": "A separate food distribution workshop",
        "expires_at": expires_at,
        "recurrence": None,
    }
    reply_args = {
        "subject": "Re: Event details",
        "body_text": "I recorded the recurring event.",
        "sent_email_summary": "confirmed that the event was recorded",
    }
    model_calls = 0

    async def retrying_model(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_event",
                        args=first_event_args,
                        tool_call_id="create-original",
                    )
                ]
            )
        if model_calls == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args=reply_args,
                        tool_call_id="reply-original",
                    )
                ]
            )
        if model_calls == 3:
            # Pydantic AI creates a server-side RetryPromptPart for this
            # unknown call, matching the retry boundary seen in the run.
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="not_a_tool",
                        args={},
                        tool_call_id="force-retry",
                    )
                ]
            )
        if model_calls == 4:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_event",
                        args=first_event_args,
                        tool_call_id="create-repeated",
                    ),
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args=reply_args,
                        tool_call_id="reply-repeated",
                    ),
                    ToolCallPart(
                        tool_name="create_event",
                        args=distinct_event_args,
                        tool_call_id="create-distinct",
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart(content="The event was recorded.")])

    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    person = MagicMock(spec=Person, email="sender@example.test")
    session.get.side_effect = lambda model, row_id: (
        person if model is Person and row_id == "person-sender" else None
    )
    stored_events: list[Event] = []

    def add(row):
        if isinstance(row, Event):
            stored_events.append(row)

    session.add.side_effect = add
    deps = AgentDeps(
        settings=Settings(
            agent_model="test:model",
            small_agent_model="test:model",
            embed_model="test:embed",
            dispatch_max_sends_per_run=10,
            dispatch_recipient_daily_cap=10,
            dispatch_sender_reply_daily_cap=10,
        ),
        sender_email="sender@example.test",
        sender_user_id="person-sender",
        sender_authenticated=True,
        trace_id="trace-replay-test",
        session_factory=lambda: session,
    )
    delivered: list[dict] = []
    consume_quota = MagicMock()
    record_memory = AsyncMock(return_value=True)

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text",
            new=MagicMock(return_value="sealed event gist"),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap", consume_quota),
        patch(
            "thenetwork.agent.tools.send_reply",
            side_effect=lambda **kwargs: delivered.append(kwargs),
        ),
        patch("thenetwork.agent.tools.record_sent_email_memory", record_memory),
    ):
        result = await build_agent(model=FunctionModel(retrying_model)).run(
            "Record this event and acknowledge it.", deps=deps
        )

    assert model_calls == 5
    assert [event.text for event in stored_events] == [
        first_event_args["text"],
        distinct_event_args["text"],
    ]
    assert len(delivered) == 1
    assert consume_quota.call_count == 2
    record_memory.assert_awaited_once()
    assert deps.outbound_send_count == 1
    assert deps.server_side_send_count == 1

    tool_returns = [
        part.content
        for message in result.all_messages()
        for part in getattr(message, "parts", ())
        if isinstance(part, ToolReturnPart)
    ]
    replayed = [value for value in tool_returns if value.get("status") == "replayed"]
    assert [value["tool_name"] for value in replayed] == [
        "create_event",
        "reply_to_sender",
    ]
    assert replayed[0]["original_result"]["status"] == "created"
    assert replayed[1]["original_result"] == {"status": "sent"}

    audit_payloads = [json.loads(record.message) for record in caplog.records]
    replay_events = [
        payload
        for payload in audit_payloads
        if payload.get("event") == "agent.tool.replayed"
    ]
    assert [payload["tool_name"] for payload in replay_events] == [
        "create_event",
        "reply_to_sender",
    ]
    assert all(payload["outcome"] == "replayed" for payload in replay_events)
    assert "sender@example.test" not in json.dumps(replay_events)
