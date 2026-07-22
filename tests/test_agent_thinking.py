from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
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
