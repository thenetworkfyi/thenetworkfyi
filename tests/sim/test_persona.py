from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.scenarios import StrongMatchScenario, default_strong_match_configs


class ScriptedTinyPerson:
    def __init__(self, name: str, replies: list[str]) -> None:
        self.name = name
        self.replies = replies
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        return {"action": {"content": self.replies.pop(0)}}


def test_tinyperson_adapter_returns_email_message_and_tracks_budget():
    person = ScriptedTinyPerson("Priya", ["I want to meet ML infra operators."])
    config = PersonaConfig(
        name="Priya Shah",
        email="priya@example.test",
        goal="Find ML infrastructure operators.",
        stop_condition="Stop after one message.",
        message_budget=1,
        agent_address="join@example.test",
    )
    adapter = TinyPersonEmailAdapter(person, config)

    msg = adapter.next_email("write", tick=7, subject="Hello")

    assert msg is not None
    assert msg["From"] == "Priya Shah <priya@example.test>"
    assert msg["To"] == "join@example.test"
    assert msg["Subject"] == "Hello"
    assert msg["X-Sim-Tick"] == "7"
    assert msg["X-Sim-Direction"] == "persona->agent"
    assert msg["X-Sim-Persona"] == "Priya Shah"
    assert "ML infra operators" in msg.get_content()
    assert adapter.next_email("again", tick=8) is None


@pytest.mark.asyncio
async def test_strong_match_scenario_replays_two_personas_to_process_email(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = (
        TinyPersonEmailAdapter(
            ScriptedTinyPerson("Priya", ["I need ML infra help in factories."]),
            configs[0],
        ),
        TinyPersonEmailAdapter(
            ScriptedTinyPerson("Samir", ["I deploy ML infra for factories."]),
            configs[1],
        ),
    )
    process = AsyncMock()

    result = await StrongMatchScenario(adapters, run_dir=tmp_path).run(process=process)

    assert result.persona_message_count == 2
    assert process.await_count == 2
    assert result.mbox_path.exists()
    assert result.transcript_path.exists()
    assert "Priya Shah" in result.transcript
    assert "Samir Vale" in result.transcript
    assert "I need ML infra help in factories." in result.transcript
    assert "I deploy ML infra for factories." in result.transcript

