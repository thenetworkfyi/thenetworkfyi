"""End-to-end unit tests for CrewAI simulation flow orchestrator and full sim suite."""

import pytest
from pydantic_ai.models.test import TestModel

from thenetwork.sim.cli import _build_person
from thenetwork.sim.personas.llm_persona import LLMTinyPerson
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.personas.population import default_population
from thenetwork.sim.run.crew_flow import NetworkSimulationFlow


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


def test_cli_person_builder_selects_crewai_backend(monkeypatch):
    crew_llm = object()
    built_person = object()
    built_with = []
    monkeypatch.setattr(
        "thenetwork.sim.personas.crew_model.build_crew_llm", lambda: crew_llm
    )
    monkeypatch.setattr(
        "thenetwork.sim.personas.crew_persona.CrewTinyPerson",
        lambda config, llm: built_with.append((config, llm)) or built_person,
    )

    population_persona = default_population()[0]
    person = _build_person(population_persona, False, True)

    assert person is built_person
    assert built_with == [(population_persona.config, crew_llm)]


@pytest.mark.asyncio
async def test_network_simulation_flow_initialization_and_run(tmp_path):
    config = _config()
    person = LLMTinyPerson(config, TestModel(custom_output_text="Hi there"))
    adapter = TinyPersonEmailAdapter(person, config)

    deliveries = []
    stage_timings = []
    progress_reports = []

    async def mock_process(**kwargs):
        pass

    flow = NetworkSimulationFlow(
        [adapter],
        run_dir=tmp_path,
        process=mock_process,
        proactive_every=None,
        progress=lambda msg: progress_reports.append(msg),
        on_delivery=lambda msg, meta: deliveries.append((msg, meta)),
        on_stage_timing=lambda event, **kwargs: stage_timings.append((event, kwargs)),
    )

    result = await flow.run(ticks=2)

    assert len(result.ticks) == 2
    assert result.ticks[0].tick == 1
    assert result.ticks[1].tick == 2
    assert flow.state.total_ticks == 2
    assert flow.state.current_tick == 2
    assert len(flow.state.completed_ticks) == 2
    assert any("started" in report for report in progress_reports)
    assert any(
        event == "sim.persona_generation_completed" for event, _ in stage_timings
    )
