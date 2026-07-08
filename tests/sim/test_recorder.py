from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from thenetwork.sim.cli import run_sim
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.recorder import SimRunConfig, SimRunRecorder
from thenetwork.sim.scenarios import default_strong_match_configs


class ScriptedTinyPerson:
    def __init__(self, body: str) -> None:
        self.name = "Scripted"
        self.body = body

    def listen_and_act(self, _stimulus: str):
        return {"content": self.body}


@pytest.mark.asyncio
async def test_run_recorder_writes_config_mbox_transcript_and_events(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = tuple(
        TinyPersonEmailAdapter(ScriptedTinyPerson(f"body {index}"), config)
        for index, config in enumerate(configs, start=1)
    )
    recorder = SimRunRecorder(
        runs_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 8, 1, 2, 3, tzinfo=timezone.utc),
    )

    artifacts = await recorder.run(
        adapters,
        SimRunConfig(
            scenario="strong-match",
            ticks=1,
            proactive_every=10,
            personas=configs,
        ),
    )

    assert artifacts.run_dir == tmp_path / "20260708T010203Z"
    assert artifacts.config_path.exists()
    assert artifacts.mbox_path.exists()
    assert artifacts.transcript_path.exists()
    assert artifacts.events_path.exists()
    config = json.loads(artifacts.config_path.read_text())
    assert config["scenario"] == "strong-match"
    assert len(config["personas"]) == 2
    assert "body 1" in artifacts.transcript_path.read_text()
    events = [
        json.loads(line)
        for line in artifacts.events_path.read_text().splitlines()
    ]
    assert events[0]["event"] == "sim.run_started"
    assert any(event["event"] == "sim.tick_completed" for event in events)
    assert events[-1]["event"] == "sim.run_completed"


@pytest.mark.asyncio
async def test_sim_run_cli_function_creates_run_directory(tmp_path):
    artifacts = await run_sim(runs_dir=tmp_path, ticks=1, proactive_every=10)

    assert artifacts.run_dir.parent == tmp_path
    assert artifacts.config_path.exists()
    assert artifacts.mbox_path.name == "all-mail.mbox"
    assert artifacts.transcript_path.name == "transcript.md"
    assert artifacts.events_path.name == "events.jsonl"
    assert len(json.loads(artifacts.config_path.read_text())["personas"]) == 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_recorder_routes_through_real_process_email(tmp_path, seeded_db):
    config = PersonaConfig(
        name="Alice",
        email="alice@test.com",
        goal="Find a collaborator for a Rust project.",
        stop_condition="An introduction is made.",
        agent_address="join@thenetwork.test",
    )
    adapter = TinyPersonEmailAdapter(
        ScriptedTinyPerson(
            "I am Alice and I am looking for a Rust collaborator for my project."
        ),
        config,
    )
    recorder = SimRunRecorder(runs_dir=tmp_path)

    with patch(
        "thenetwork.worker.tasks.run_agent_for_email", AsyncMock()
    ) as mock_agent:
        artifacts = await recorder.run(
            (adapter,),
            SimRunConfig(
                scenario="real-process",
                ticks=1,
                proactive_every=None,
                personas=(config,),
                mock_process=False,
            ),
        )

    mock_agent.assert_called_once()
    called_kwargs = mock_agent.call_args.kwargs
    assert called_kwargs["sender_email"] == "alice@test.com"
    assert called_kwargs["sender_user_id"] == seeded_db["alice_id"]

    written_config = json.loads(artifacts.config_path.read_text())
    assert written_config["process_mode"] == "real"

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    event_names = [event["event"] for event in events]
    assert "sim.process_email_started" in event_names
    assert "sim.process_email_completed" in event_names
    assert "sim.mock_process_email" not in event_names
