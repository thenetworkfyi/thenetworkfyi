from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentDeps
from thenetwork.settings import Settings


def test_agent_thinking_level_defaults_to_medium():
    settings = Settings(
        _env_file=None,
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
    )

    assert settings.agent_thinking_level == "medium"


def test_agent_thinking_level_can_be_disabled():
    settings = Settings(
        _env_file=None,
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
        agent_thinking_level=None,
    )

    assert settings.agent_thinking_level is None


def test_build_agent_applies_configured_thinking_level(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.agent.core.get_settings",
        lambda: SimpleNamespace(agent_model=TestModel(), agent_thinking_level="high"),
    )

    agent = build_agent()

    assert agent.model_settings == {"thinking": "high"}


def test_build_agent_omits_thinking_settings_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.agent.core.get_settings",
        lambda: SimpleNamespace(agent_model=TestModel(), agent_thinking_level=None),
    )

    agent = build_agent()

    assert agent.model_settings is None


@pytest.mark.asyncio
async def test_no_action_output_ends_run_without_another_model_request():
    request_count = 0

    async def call_no_action(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal request_count
        request_count += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="no_action",
                    args={"reason": "nothing useful to do"},
                )
            ]
        )

    agent = build_agent(model=FunctionModel(call_no_action))

    result = await agent.run("FYI", deps=AgentDeps())

    assert result.output == ""
    assert request_count == 1
    assert result.usage.requests == 1
    tool_calls = [
        part
        for message in result.all_messages()
        for part in message.parts
        if getattr(part, "part_kind", None) == "tool-call"
    ]
    assert [part.tool_name for part in tool_calls] == ["no_action"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_id", "event_id", "expected_tools"),
    [
        ("person-candidate", None, ["propose_introduction"]),
        (None, "event-bound", ["send_event_recommendation"]),
        (None, None, []),
    ],
)
async def test_proactive_agent_exposes_only_its_bound_action(
    candidate_id: str | None,
    event_id: str | None,
    expected_tools: list[str],
):
    observed_tools: list[str] = []

    async def capture_tools(
        _messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        observed_tools.extend(tool.name for tool in info.function_tools)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="no_action",
                    args={"reason": "no supported proactive action"},
                )
            ]
        )

    agent = build_agent(
        model=FunctionModel(capture_tools),
        is_proactive=True,
        proactive_candidate_id=candidate_id,
        proactive_event_id=event_id,
    )
    deps = AgentDeps(
        is_proactive=True,
        proactive_candidate_id=candidate_id,
        proactive_event_id=event_id,
    )

    result = await agent.run("Synthetic trigger", deps=deps)

    assert observed_tools == expected_tools
    assert result.output == ""


@pytest.mark.asyncio
async def test_inbound_agent_retains_first_contact_and_full_tool_surface():
    observed_tools: set[str] = set()

    async def capture_tools(
        _messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        observed_tools.update(tool.name for tool in info.function_tools)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="no_action",
                    args={"reason": "nothing useful to do"},
                )
            ]
        )

    result = await build_agent(model=FunctionModel(capture_tools)).run(
        "FYI", deps=AgentDeps()
    )

    assert observed_tools == {
        "remember",
        "forget",
        "search",
        "propose_introduction",
        "escalate",
        "send_first_contact_welcome",
        "reply_to_sender",
        "send_outreach",
        "register_person",
        "create_event",
        "update_event",
        "cancel_event",
        "search_events",
        "send_event_recommendation",
        "stop_event_recommendations",
        "resume_event_recommendations",
    }
    assert result.output == ""


@pytest.mark.asyncio
async def test_large_bare_text_is_retried_into_explicit_no_action():
    calls = 0

    async def invalid_then_no_action(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[TextPart(content="x" * 50_000)])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="no_action",
                    args={"reason": "nothing useful to do"},
                )
            ]
        )

    result = await build_agent(model=FunctionModel(invalid_then_no_action)).run(
        "FYI", deps=AgentDeps()
    )

    assert calls == 2
    assert result.usage.requests == 2
    assert result.output == ""


@pytest.mark.asyncio
async def test_successful_dispatch_allows_concise_final_operator_text():
    calls = 0

    async def reply_then_finish(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args={
                            "subject": "Re: Question",
                            "body_text": "A bounded reply.",
                            "sent_email_summary": "answered the sender's question",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Reply dispatched.")])

    deps = AgentDeps(
        settings=Settings(
            _env_file=None,
            agent_model="test:model",
            small_agent_model="test:model",
            embed_model="test:embed",
            dispatch_max_sends_per_run=3,
            dispatch_recipient_daily_cap=10,
            dispatch_sender_reply_daily_cap=10,
        ),
        sender_email="new@example.com",
        sender_authenticated=True,
    )
    with (
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap"),
        patch("thenetwork.agent.tools.send_reply") as send,
    ):
        result = await build_agent(model=FunctionModel(reply_then_finish)).run(
            "What does this do?", deps=deps
        )

    assert calls == 2
    assert result.output == "Reply dispatched."
    assert deps.server_side_send_count == 1
    send.assert_called_once()
