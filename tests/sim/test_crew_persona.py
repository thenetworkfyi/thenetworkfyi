"""Unit tests for CrewAI persona agent, task builder, and response parser."""

import pytest
from crewai import LLM

from thenetwork.sim.personas.crew_persona import (
    PASS_SENTINEL,
    build_persona_agent,
    build_persona_turn_task,
    extract_persona_response,
)
from thenetwork.sim.personas.persona import PersonaConfig


def _config(**overrides) -> PersonaConfig:
    defaults = dict(
        name="Priya Shah",
        email="priya@example.test",
        goal="Find ML infrastructure operators.",
        stop_condition="Stop once introduced.",
        message_budget=3,
        agent_address="join@example.test",
    )
    defaults.update(overrides)
    return PersonaConfig(**defaults)


def test_build_persona_agent_configures_persona_fields():
    llm = LLM(model="gpt-4o", api_key="test-key")
    config = _config()
    agent = build_persona_agent(config, llm, memory=False)

    assert agent.role == "Priya Shah <priya@example.test>"
    assert agent.goal == "Find ML infrastructure operators."
    assert "Priya Shah" in agent.backstory
    assert "priya@example.test" in agent.backstory
    assert "join@example.test" in agent.backstory
    assert "Stop once introduced." in agent.backstory
    assert agent.allow_delegation is False


def test_build_persona_turn_task_includes_stimulus_and_instructions():
    llm = LLM(model="gpt-4o", api_key="test-key")
    config = _config()
    agent = build_persona_agent(config, llm)
    stimulus = "Tick 1. Welcome to The Network."
    task = build_persona_turn_task(agent, stimulus, tick=1)

    assert "Current simulation tick: 1" in task.description
    assert stimulus in task.description
    assert "[intro:...]" in task.description
    assert PASS_SENTINEL in task.description
    assert task.agent == agent


@pytest.mark.parametrize(
    "raw_output,expected_content",
    [
        ("Hello, I run ML platforms.", "Hello, I run ML platforms."),
        ("YES\n[intro:123]", "YES\n[intro:123]"),
        ("PASS", ""),
        ("PASS\nExtra text", ""),
        ("pass", ""),
        ("", ""),
    ],
)
def test_extract_persona_response(raw_output: str, expected_content: str):
    res = extract_persona_response(raw_output)
    assert res == {"content": expected_content}


def test_extract_persona_response_from_object_with_raw_attribute():
    class DummyTaskOutput:
        raw = "   Hi there!   "

    res = extract_persona_response(DummyTaskOutput())
    assert res == {"content": "Hi there!"}
