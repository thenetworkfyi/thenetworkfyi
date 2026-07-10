from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from thenetwork.audit import audit_event
from thenetwork.db.models import IntroductionConsent, Memory, Person
from thenetwork.sim.cli import main, run_sim
from thenetwork.sim.compare import compare_runs, load_run_metrics
from thenetwork.sim.mail import SimPostOffice
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.population import DEFAULT_OUTCOME_CHECKS
from thenetwork.sim.recorder import (
    SimRunArtifacts,
    SimRunConfig,
    SimRunRecorder,
    _assemble_scenario_outcome,
    _config_payload,
    _database_outcome_state,
)
from thenetwork.sim.scenarios import default_strong_match_configs
from thenetwork.sim.scoring import IntroductionRevealAuthorization, OutcomeCheck


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
    assert artifacts.audit_path.name == "audit.jsonl"
    assert not artifacts.audit_path.exists()
    config = json.loads(artifacts.config_path.read_text())
    assert len(config["personas"]) == 17
    assert len(config["outcome_checks"]) == len(DEFAULT_OUTCOME_CHECKS)
    assert config["llm_personas"] is False
    events = [json.loads(line) for line in artifacts.events_path.read_text().splitlines()]
    assert {"sim.score.tier1", "sim.score.tier2", "sim.score.outcome"} <= {
        event["event"] for event in events
    }


@pytest.mark.asyncio
async def test_sim_run_persona_cap_remains_backward_compatible(tmp_path):
    artifacts = await run_sim(
        runs_dir=tmp_path,
        ticks=1,
        proactive_every=None,
        personas=10,
    )

    config = json.loads(artifacts.config_path.read_text())
    assert len(config["personas"]) == 10


@pytest.mark.asyncio
async def test_real_process_run_uses_and_records_per_run_database(tmp_path):
    expected = SimpleNamespace(run_dir=tmp_path / "run")
    database_name = "sim_0123456789abcdef"

    with (
        patch(
            "thenetwork.sim.cli.new_sim_database_name",
            return_value=database_name,
        ),
        patch(
            "thenetwork.sim.cli.provision_sim_database",
            return_value=nullcontext(database_name),
        ) as provision,
        patch.object(
            SimRunRecorder,
            "run",
            AsyncMock(return_value=expected),
        ) as record,
    ):
        artifacts = await run_sim(
            runs_dir=tmp_path,
            ticks=1,
            proactive_every=None,
            mock_process=False,
            keep_db=True,
            personas=1,
        )

    assert artifacts is expected
    provision.assert_called_once_with(database_name, keep=True)
    recorded_config = record.await_args.args[1]
    assert recorded_config.mock_process is False
    assert recorded_config.database_name == database_name


@pytest.mark.asyncio
async def test_keep_db_is_rejected_for_mock_run(tmp_path):
    with pytest.raises(ValueError, match="keep_db requires mock_process=False"):
        await run_sim(
            runs_dir=tmp_path,
            ticks=1,
            proactive_every=None,
            keep_db=True,
        )


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
async def test_real_process_runs_capture_isolated_traceable_audit_logs(
    tmp_path, capsys
):
    persona_config = PersonaConfig(
        name="Alice",
        email="alice@test.com",
        goal="Find a collaborator for a Rust project.",
        stop_condition="An introduction is made.",
        agent_address="join@example.test",
    )
    adapters = (
        TinyPersonEmailAdapter(ScriptedTinyPerson("body 1"), persona_config),
    )
    config = SimRunConfig(
        scenario="real-process",
        ticks=1,
        proactive_every=None,
        personas=(persona_config,),
        mock_process=False,
    )

    async def audited_process(**kwargs):
        audit_event(
            "agent.tool.completed",
            tool_name="remember",
            trace_id=kwargs["trace_id"],
        )

    clock_calls = iter(
        [
            datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 8, 1, 1, 0, tzinfo=timezone.utc),
        ]
    )
    recorder = SimRunRecorder(runs_dir=tmp_path, clock=lambda: next(clock_calls))

    with patch("thenetwork.sim.recorder.process_email.func", new=audited_process):
        first = await recorder.run(adapters, config)
        second = await recorder.run(adapters, config)

    for artifacts in (first, second):
        audit_events = [
            json.loads(line) for line in artifacts.audit_path.read_text().splitlines()
        ]
        process_events = [
            json.loads(line)
            for line in artifacts.events_path.read_text().splitlines()
            if "process_email_started" in line
        ]
        assert len(audit_events) == 1
        assert audit_events[0]["event"] == "agent.tool.completed"
        assert audit_events[0]["trace_id"] == process_events[0]["trace_id"]
    assert first.audit_path.read_text() != ""
    assert second.audit_path.read_text() != ""
    assert capsys.readouterr().err == ""


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
async def test_mock_recorder_writes_one_skipped_default_outcome_score(tmp_path):
    config = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Find a collaborator.",
        stop_condition="A connection is made.",
        agent_address="join@example.test",
    )
    artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
        (TinyPersonEmailAdapter(ScriptedTinyPerson("Looking for peers."), config),),
        SimRunConfig(
            scenario="mock-defaults",
            ticks=1,
            proactive_every=None,
            personas=(config,),
            outcome_checks=DEFAULT_OUTCOME_CHECKS,
        ),
    )

    events = [json.loads(line) for line in artifacts.events_path.read_text().splitlines()]
    outcome_events = [event for event in events if event["event"] == "sim.score.outcome"]
    assert len(outcome_events) == 1
    assert len(outcome_events[0]["findings"]) == len(DEFAULT_OUTCOME_CHECKS)
    assert outcome_events[0]["passed"] is True
    assert all(
        finding["evidence"] == {"skipped": True}
        for finding in outcome_events[0]["findings"]
    )


def test_outcome_assembly_reads_fixture_mail_audit_and_database_state(tmp_path):
    artifacts = SimRunArtifacts(
        run_dir=tmp_path,
        config_path=tmp_path / "config.json",
        mbox_path=tmp_path / "all-mail.mbox",
        transcript_path=tmp_path / "transcript.md",
        events_path=tmp_path / "events.jsonl",
        audit_path=tmp_path / "audit.jsonl",
    )
    message = EmailMessage()
    message["From"] = "join@example.test"
    message["To"] = "nadia.sim@example.test"
    message["Subject"] = "A note"
    message.set_content("Bakery supply co-op update")
    SimPostOffice(mbox_path=artifacts.mbox_path).deliver(message)
    artifacts.audit_path.write_text(
        json.dumps({"event": "introduction.consent_transition", "action": "clarify"})
        + "\n",
        encoding="utf-8",
    )
    rows = (
        IntroductionRevealAuthorization(
            person_a_email="omar.sim@example.test",
            person_b_email="peer@example.test",
            status="one_consented",
        ),
    )
    memories = (Memory(id="memory-1", text="raw", refs=["nadia-id"], gist="bakery"),)
    memory_counts = {"nadia.sim@example.test": 1}

    with patch(
        "thenetwork.sim.recorder._database_outcome_state",
        return_value=(rows, memories, memory_counts),
    ):
        outcome, assembled_memories = _assemble_scenario_outcome(
            artifacts,
            memories=(),
            load_database_state=True,
        )

    assert outcome.consent_rows == rows
    assert outcome.audit_events == (
        {"event": "introduction.consent_transition", "action": "clarify"},
    )
    assert outcome.mail_facts[0].recipients == frozenset({"nadia.sim@example.test"})
    assert outcome.mail_facts[0].body == "Bakery supply co-op update\n"
    assert outcome.memory_counts == memory_counts
    assert assembled_memories == memories


def test_database_outcome_state_materializes_values_before_session_closes():
    class ExpiringPerson:
        def __init__(self, person_id: str, email: str) -> None:
            self._id = person_id
            self._email = email
            self.detached = False

        @property
        def id(self) -> str:
            if self.detached:
                raise RuntimeError("detached person id")
            return self._id

        @property
        def email(self) -> str:
            if self.detached:
                raise RuntimeError("detached person email")
            return self._email

    class ExpiringMemory:
        def __init__(self) -> None:
            self.detached = False

        def _value(self, name: str, value):
            if self.detached:
                raise RuntimeError(f"detached memory {name}")
            return value

        @property
        def id(self) -> str:
            return self._value("id", "memory-1")

        @property
        def text(self) -> str:
            return self._value("text", "raw")

        @property
        def refs(self) -> list[str]:
            return self._value("refs", ["nadia-id"])

        @property
        def gist(self) -> str:
            return self._value("gist", "bakery")

    nadia = ExpiringPerson("nadia-id", "nadia.sim@example.test")
    peer = ExpiringPerson("peer-id", "peer@example.test")
    memory = ExpiringMemory()
    consent = SimpleNamespace(
        person_a_id="nadia-id",
        person_b_id="peer-id",
        status="introduced",
    )

    class Result:
        def __init__(self, rows) -> None:
            self.rows = rows

        def all(self):
            return self.rows

    class Session:
        def exec(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            return Result(
                {
                    IntroductionConsent: [consent],
                    Memory: [memory],
                    Person: [nadia, peer],
                }[entity]
            )

        def get(self, model, row_id):
            assert model is Person
            return {"nadia-id": nadia, "peer-id": peer}[row_id]

    @contextmanager
    def session_context():
        try:
            yield Session()
        finally:
            nadia.detached = True
            peer.detached = True
            memory.detached = True

    with patch("thenetwork.sim.recorder.get_session", session_context):
        consent_rows, memories, memory_counts = _database_outcome_state()

    assert consent_rows[0].participant_emails == frozenset(
        {"nadia.sim@example.test", "peer@example.test"}
    )
    assert memories[0].gist == "bakery"
    assert memories[0].refs == ["nadia-id"]
    assert memory_counts == {"nadia.sim@example.test": 1}


def test_config_payload_keeps_outcome_check_metadata_without_predicates():
    config = SimRunConfig(
        scenario="metadata",
        ticks=1,
        proactive_every=None,
        personas=(),
        outcome_checks=(
            OutcomeCheck(
                description="callable-free metadata",
                predicate=lambda _outcome: True,
                requires_real_process=True,
            ),
        ),
    )

    payload = _config_payload(config, "mock")

    assert payload["outcome_checks"] == [
        {
            "description": "callable-free metadata",
            "requires_real_process": True,
            "requires_llm_personas": False,
        }
    ]


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
