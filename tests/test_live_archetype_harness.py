"""Offline contracts for the live-model archetype harness."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tests.scenarios.test_live_archetypes import EmailScenario, run_scenario


@pytest.mark.asyncio
async def test_forced_escalation_is_captured_without_outbound_infrastructure():
    model_calls = 0

    async def force_escalation(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="escalate",
                        args={"reason": "Needs human judgment"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Escalated for review.")])

    with (
        patch(
            "thenetwork.email.outbound.send_reply",
            side_effect=AssertionError("real outbound delivery was attempted"),
        ),
        patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("DNS resolution was attempted"),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
    ):
        outcome = await run_scenario(
            EmailScenario(
                subject="Please review this",
                body="I need a human decision.",
                sender_email="sender@example.com",
                sender_user_id="person-sender",
                sender_authenticated=True,
                admin_emails=["admin@example.com"],
            ),
            model=FunctionModel(force_escalation),
        )

    assert outcome.escalated == ["Needs human judgment"]
    assert outcome.dispatched == []


@pytest.mark.asyncio
async def test_forced_proposal_uses_captured_fixed_deliveries():
    model_calls = 0

    async def force_proposal(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="propose_introduction",
                        args={
                            "other_person_id": "person-other",
                            "sender_gist": "Rust systems programmer",
                            "other_gist": "Rust storage engineer",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Proposal recorded.")])

    with (
        patch(
            "thenetwork.email.outbound.send_reply",
            side_effect=AssertionError("real outbound delivery was attempted"),
        ),
        patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("DNS resolution was attempted"),
        ),
    ):
        outcome = await run_scenario(
            EmailScenario(
                subject="Rust systems folks",
                body="I would like to meet this person.",
                sender_email="sender@example.com",
                sender_user_id="person-sender",
                sender_authenticated=True,
                known_people={"person-other": "other@example.com"},
            ),
            model=FunctionModel(force_proposal),
        )

    assert "propose_introduction" in outcome.tool_calls
    assert {delivery["to"] for delivery in outcome.dispatched} == {
        "sender@example.com",
        "other@example.com",
    }
