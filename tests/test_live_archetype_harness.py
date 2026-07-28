"""Offline contracts for the live-model archetype harness."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from thenetwork.search.match import MemoryMatch
from tests.scenarios.test_live_archetypes import EmailScenario, run_scenario

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_forced_escalation_is_captured_without_outbound_infrastructure(
    scenario_database,
):
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
            scenario_database=scenario_database,
            model=FunctionModel(force_escalation),
        )

    assert outcome.escalated == ["Needs human judgment"]
    assert outcome.dispatched == []


@pytest.mark.asyncio
async def test_forced_proposal_uses_captured_fixed_deliveries(scenario_database):
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
            scenario_database=scenario_database,
            model=FunctionModel(force_proposal),
        )

    assert "propose_introduction" in outcome.tool_calls
    assert {delivery["to"] for delivery in outcome.dispatched} == {
        "sender@example.com",
        "other@example.com",
    }


@pytest.mark.asyncio
async def test_grouped_candidate_evidence_can_drive_a_bound_proposal(
    scenario_database,
):
    model_calls = 0

    async def inspect_then_propose(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="search",
                        args={"query": "Rust systems peer"},
                    )
                ]
            )
        if model_calls == 2:
            search_result = next(
                part.content
                for message in reversed(messages)
                for part in reversed(message.parts)
                if isinstance(part, ToolReturnPart) and part.tool_name == "search"
            )
            assert len(search_result) == 1
            candidate = search_result[0]
            assert candidate["person_id"] == "person-other"
            gists = [item["gist"] for item in candidate["evidence"]]
            assert gists == [
                "wants Rust systems peers",
                "builds a distributed storage engine in Rust",
                "works deeply on storage internals",
            ]
            assert all(set(item) == {"gist"} for item in candidate["evidence"])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="propose_introduction",
                        args={
                            "other_person_id": candidate["person_id"],
                            "sender_gist": "Rust systems programmer seeking peers",
                            "other_gist": "; ".join(gists),
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Proposal recorded.")])

    outcome = await run_scenario(
        EmailScenario(
            subject="Rust systems folks",
            body="I write low-level Rust infrastructure and want relevant peers.",
            sender_email="sender@example.com",
            sender_user_id="person-sender",
            sender_authenticated=True,
            known_people={"person-other": "other@example.com"},
            search_results=[
                MemoryMatch(
                    "other-intent",
                    "person-other",
                    "wants Rust systems peers",
                    0.93,
                ),
                MemoryMatch(
                    "other-contribution",
                    "person-other",
                    "builds a distributed storage engine in Rust",
                    0.91,
                ),
                MemoryMatch(
                    "other-scope",
                    "person-other",
                    "works deeply on storage internals",
                    0.88,
                ),
            ],
        ),
        scenario_database=scenario_database,
        model=FunctionModel(inspect_then_propose),
    )

    assert "search" in outcome.tool_calls
    assert "propose_introduction" in outcome.tool_calls
    assert outcome.tool_calls.index("search") < outcome.tool_calls.index(
        "propose_introduction"
    )
    assert {delivery["to"] for delivery in outcome.dispatched} == {
        "sender@example.com",
        "other@example.com",
    }
