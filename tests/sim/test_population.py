from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from thenetwork.sim.loop import SimTickLoop
from thenetwork.sim.persona import TinyPersonEmailAdapter
from thenetwork.sim.population import SimSchedule, default_population


class RecordingTinyPerson:
    def __init__(self, body: str) -> None:
        self.name = "Recording"
        self.body = body
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        return {"content": self.body}


def test_default_population_has_authored_personas_and_schedule():
    population = default_population(agent_address="join@example.test")

    assert 8 <= len(population) <= 15
    assert len({persona.config.email for persona in population}) == len(population)
    assert all(persona.opening_body for persona in population)

    schedule = SimSchedule.from_population(population)
    assert any(event.kind == "intervention" for event in schedule.events)
    assert any(interruption.kind == "silence" for interruption in schedule.interruptions)
    assert any(interruption.kind == "dormancy" for interruption in schedule.interruptions)


@pytest.mark.asyncio
async def test_tick_loop_skips_mechanical_interruptions(tmp_path):
    population = default_population(agent_address="join@example.test")
    mara = next(persona for persona in population if persona.config.name == "Mara Vidal")
    person = RecordingTinyPerson("Mara is back.")
    adapter = TinyPersonEmailAdapter(person, mara.config)

    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        process=AsyncMock(),
        proactive_every=None,
        schedule=SimSchedule.from_population((mara,)),
    )

    result = await loop.run(ticks=4)

    assert result.persona_messages == 1
    assert len(person.stimuli) == 1


@pytest.mark.asyncio
async def test_tick_loop_includes_scheduled_events_in_prompt(tmp_path):
    population = default_population(agent_address="join@example.test")
    nora = next(persona for persona in population if persona.config.name == "Nora Chen")
    person = RecordingTinyPerson("I have an update.")
    adapter = TinyPersonEmailAdapter(person, nora.config)

    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        process=AsyncMock(),
        proactive_every=None,
        schedule=SimSchedule.from_population((nora,)),
    )

    await loop.run(ticks=3)

    assert any("cement plant in Lisbon" in stimulus for stimulus in person.stimuli)

