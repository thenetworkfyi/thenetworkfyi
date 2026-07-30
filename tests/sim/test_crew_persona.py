"""Unit tests for CrewAI persona agent, task builder, and response parser."""

import asyncio
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from crewai import LLM

from thenetwork.sim.personas.crew_persona import (
    PASS_SENTINEL,
    CrewTinyPerson,
    build_persona_agent,
    build_persona_turn_task,
    extract_persona_response,
)
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.run.loop import SimTickLoop
from thenetwork.sim.run.mail import SimPostOffice, _extract_body


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


def test_crew_persona_binds_mailbox_tool_to_real_agent():
    person = CrewTinyPerson(_config(), LLM(model="gpt-4o", api_key="test-key"))
    post_office = SimPostOffice()

    person.prepare_turn(post_office=post_office, tick=4, reply_to=None)

    assert person.mailbox_tool is not None
    assert person.mailbox_tool.post_office is post_office
    assert person.mailbox_tool.tick == 4
    assert person.mailbox_tool.allow_send is False
    assert person.agent.tools == [person.mailbox_tool]
    assert "managed by the simulation runtime" in person.mailbox_tool._run(
        action="send", body="would duplicate"
    )
    assert post_office.messages_for(person.config.agent_address) == ()


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


def test_extract_persona_response_rejects_arbitrary_objects():
    with pytest.raises(TypeError, match="string response"):
        extract_persona_response(object())


@pytest.mark.asyncio
async def test_crew_persona_runtime_turns_are_async_and_preserve_consent_and_budget(
    monkeypatch, tmp_path
):
    active_token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    other_token = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    started: list[str] = []
    both_started = asyncio.Event()

    class FakeAgent:
        def __init__(self, name):
            self.role = name
            self.tools = []

    class FakeTask:
        def __init__(self, agent):
            self.agent = agent

        async def execute_async(self):
            started.append(self.agent.role)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return f"YES\n[intro:{other_token}]"

    monkeypatch.setattr(
        "thenetwork.sim.personas.crew_persona.build_persona_agent",
        lambda config, _llm: FakeAgent(config.name),
    )
    monkeypatch.setattr(
        "thenetwork.sim.personas.crew_persona.build_persona_turn_task",
        lambda agent, _stimulus, tick=None: FakeTask(agent),
    )

    adapters = tuple(
        TinyPersonEmailAdapter(
            CrewTinyPerson(config, SimpleNamespace()),
            config,
        )
        for config in (
            _config(
                name="Priya",
                email="priya@example.test",
                message_budget=1,
            ),
            _config(
                name="Noor",
                email="noor@example.test",
                message_budget=1,
            ),
        )
    )
    process = AsyncMock()
    loop = SimTickLoop(
        adapters,
        run_dir=tmp_path,
        process=process,
        proactive_every=None,
        turn_concurrency=2,
    )
    for adapter in adapters:
        request = EmailMessage()
        request["From"] = "join@example.test"
        request["To"] = adapter.config.email
        request["Subject"] = f"Possible introduction [intro:{active_token}]"
        request["Message-ID"] = f"<{adapter.config.name}@example.test>"
        request.set_content("Do you accept?")
        loop.post_office.deliver(request)

    result = await loop.run(ticks=2)

    assert result.persona_messages == 2
    assert process.await_count == 2
    assert set(started) == {"Priya", "Noor"}
    assert len(loop.post_office.messages_for("join@example.test")) == 2
    for adapter in adapters:
        person = adapter.person
        assert person.mailbox_tool is not None
        assert person.agent.tools == [person.mailbox_tool]
        (message,) = loop.post_office.messages_for(adapter.config.agent_address)[:1]
        body = _extract_body(message)
        assert f"[intro:{active_token}]" in body
        assert f"[intro:{other_token}]" not in body
