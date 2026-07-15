from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.run.loop import SimTickLoop, run_proactive_scans
from thenetwork.sim.run.recorder import EventsLog, _recording_process
from thenetwork.worker import proactive


class FailingPersona:
    name = "Failing persona"

    async def alisten_and_act(self, _stimulus: str) -> str:
        raise RuntimeError("persona failed")


def _timing_events(observer: Mock) -> list[tuple[str, dict]]:
    return [(call.args[0], call.kwargs) for call in observer.call_args_list]


@pytest.mark.asyncio
async def test_persona_generation_records_duration_when_persona_fails(tmp_path):
    observer = Mock()
    adapter = TinyPersonEmailAdapter(
        FailingPersona(),
        PersonaConfig(
            name="Failing persona",
            email="persona@example.test",
            goal="Test timing.",
            stop_condition="Never.",
            agent_address="join@example.test",
        ),
    )
    loop = SimTickLoop(
        (adapter,),
        run_dir=tmp_path,
        proactive_every=None,
        on_stage_timing=observer,
    )

    with pytest.raises(RuntimeError, match="persona failed"):
        await loop.run(ticks=1)

    event, fields = _timing_events(observer).pop()
    assert event == "sim.persona_generation_completed"
    assert fields["error_type"] == "RuntimeError"
    assert fields["persona"] == "Failing persona"
    assert fields["status"] == "failed"
    assert fields["tick"] == 1
    assert isinstance(fields["elapsed_ms"], int)
    assert fields["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_proactive_job_records_duration_when_processing_fails():
    observer = Mock()

    async def scan(_timestamp: int) -> None:
        proactive.process_email.defer(trace_id="trace-1")

    async def failing_process(**_kwargs) -> None:
        raise RuntimeError("job failed")

    with pytest.raises(RuntimeError, match="job failed"):
        await run_proactive_scans(
            timestamp=1,
            scans=(scan,),
            process=failing_process,
            on_stage_timing=observer,
        )

    event, fields = _timing_events(observer).pop()
    assert event == "sim.proactive_job_processed"
    assert fields["error_type"] == "RuntimeError"
    assert fields["status"] == "failed"
    assert fields["trace_id"] == "trace-1"
    assert isinstance(fields["elapsed_ms"], int)
    assert fields["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_process_email_failure_event_includes_duration(tmp_path):
    events_path = tmp_path / "events.jsonl"

    async def failing_process(**_kwargs) -> None:
        raise RuntimeError("process failed")

    await _recording_process(failing_process, EventsLog(events_path))(
        sender_email="person@example.test",
        subject="subject",
        trace_id="trace-1",
    )

    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    event = events[-1]
    assert event["event"] == "sim.process_email_failed"
    assert event["error"] == "process failed"
    assert event["error_type"] == "RuntimeError"
    assert isinstance(event["elapsed_ms"], int)
    assert event["elapsed_ms"] >= 0
