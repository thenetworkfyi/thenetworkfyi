from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from thenetwork.sim.cli import main, run_sim
from thenetwork.sim.compare import compare_runs, load_run_metrics
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


def test_sim_run_cli_streams_progress_to_stderr_and_only_path_to_stdout(
    tmp_path, capsys
):
    main(
        [
            "run",
            "--runs-dir",
            str(tmp_path),
            "--ticks",
            "2",
            "--personas",
            "1",
        ]
    )

    captured = capsys.readouterr()
    stdout_lines = captured.out.splitlines()
    assert len(stdout_lines) == 1
    assert Path(stdout_lines[0]).parent == tmp_path
    assert captured.err.splitlines() == [
        "tick 1/2: started",
        "tick 1/2: Priya Shah: process_email started",
        "tick 1/2: Priya Shah: process_email completed",
        "tick 1/2: completed (1 persona messages, 0 proactive jobs)",
        "tick 2/2: started",
        "tick 2/2: Priya Shah: process_email started",
        "tick 2/2: Priya Shah: process_email completed",
        "tick 2/2: completed (1 persona messages, 0 proactive jobs)",
    ]


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


@pytest.mark.asyncio
async def test_run_recorder_writes_tier1_score_before_run_completed(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = tuple(
        TinyPersonEmailAdapter(ScriptedTinyPerson(f"body {index}"), config)
        for index, config in enumerate(configs, start=1)
    )
    recorder = SimRunRecorder(runs_dir=tmp_path)

    artifacts = await recorder.run(
        adapters,
        SimRunConfig(scenario="strong-match", ticks=1, proactive_every=10, personas=configs),
    )

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    tier1_events = [event for event in events if event["event"] == "sim.score.tier1"]
    assert len(tier1_events) == 1
    assert tier1_events[0]["passed"] is True
    assert events[-1]["event"] == "sim.run_completed"


@pytest.mark.asyncio
async def test_run_recorder_does_not_multiply_or_drop_outcome_metrics(tmp_path):
    persona_config = PersonaConfig(
        name="Alice",
        email="alice@test.com",
        goal="Find a collaborator for a Rust project.",
        stop_condition="An introduction is made.",
        agent_address="join@example.test",
    )
    adapters = (TinyPersonEmailAdapter(ScriptedTinyPerson("body 1"), persona_config),)
    config = SimRunConfig(
        scenario="strong-match", ticks=1, proactive_every=None, personas=(persona_config,)
    )

    async def two_tool_calls(**_kwargs):
        return {
            "tool_calls": ("remember", "dispatch_email"),
            "total_tokens": 100,
            "cost_usd": 0.02,
        }

    async def zero_tool_calls_with_tokens(**_kwargs):
        return {"tool_calls": (), "total_tokens": 50, "cost_usd": 0.01}

    recorder = SimRunRecorder(runs_dir=tmp_path)
    multi_tool = await recorder.run(adapters, config, process=two_tool_calls)
    zero_tool = await recorder.run(adapters, config, process=zero_tool_calls_with_tokens)

    multi_metrics = load_run_metrics(multi_tool.run_dir)
    zero_metrics = load_run_metrics(zero_tool.run_dir)

    assert multi_metrics.token_usage == 100
    assert multi_metrics.cost_usd == pytest.approx(0.02)
    assert zero_metrics.token_usage == 50
    assert zero_metrics.cost_usd == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_two_recorded_runs_produce_a_comparable_non_empty_delta(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = tuple(
        TinyPersonEmailAdapter(ScriptedTinyPerson(f"body {index}"), config)
        for index, config in enumerate(configs, start=1)
    )

    async def quiet_process(**_kwargs):
        return {"judge_score": 0}

    async def busy_process(**_kwargs):
        return {
            "tool_calls": ("dispatch_email",),
            "total_tokens": 120,
            "cost_usd": 0.01,
            "judge_score": 8,
        }

    clock_calls = iter(
        [
            datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=1),
        ]
    )
    recorder = SimRunRecorder(runs_dir=tmp_path, clock=lambda: next(clock_calls))
    config = SimRunConfig(scenario="strong-match", ticks=1, proactive_every=10, personas=configs)

    before = await recorder.run(adapters, config, process=quiet_process)
    after = await recorder.run(adapters, config, process=busy_process)

    deltas = compare_runs(before.run_dir, after.run_dir)
    delta_by_name = {delta.name: delta.delta for delta in deltas}
    assert delta_by_name["introductions"] not in ("+0", "n/a")
    assert delta_by_name["judge_score"] not in ("n/a", "+0.00")
    assert delta_by_name["token_usage"] not in ("+0", "n/a")
    assert delta_by_name["cost_usd"] not in ("+0.0000", "n/a")
    assert any(delta != "+0" and delta != "n/a" for delta in delta_by_name.values())
