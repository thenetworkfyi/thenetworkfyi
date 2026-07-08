"""Run recording for simulation harness executions."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from thenetwork.sim.loop import SimTickLoop
from thenetwork.sim.mail import render_transcript
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.population import SimSchedule
from thenetwork.worker.tasks import process_email


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SimRunConfig:
    scenario: str
    ticks: int
    proactive_every: int | None
    personas: tuple[PersonaConfig, ...]
    mock_process: bool = True


@dataclass(frozen=True)
class SimRunArtifacts:
    run_dir: Path
    config_path: Path
    mbox_path: Path
    transcript_path: Path
    events_path: Path


class EventsLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")


class SimRunRecorder:
    """Creates run directories and writes config, mbox, transcript, events."""

    def __init__(
        self,
        *,
        runs_dir: Path = Path("runs"),
        clock: Clock | None = None,
    ) -> None:
        self.runs_dir = runs_dir
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(
        self,
        adapters: tuple[TinyPersonEmailAdapter, ...],
        config: SimRunConfig,
        *,
        process=None,
        schedule: SimSchedule | None = None,
    ) -> SimRunArtifacts:
        run_dir = self._new_run_dir()
        artifacts = SimRunArtifacts(
            run_dir=run_dir,
            config_path=run_dir / "config.json",
            mbox_path=run_dir / "all-mail.mbox",
            transcript_path=run_dir / "transcript.md",
            events_path=run_dir / "events.jsonl",
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        events = EventsLog(artifacts.events_path)

        if process is not None:
            process_mode = "override"
            process_func = process
        elif config.mock_process is False:
            process_mode = "real"
            process_func = process_email.func
        else:
            process_mode = "mock"
            process_func = _mock_process(events)

        artifacts.config_path.write_text(
            json.dumps(
                _config_payload(config, process_mode), indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        events.write("sim.run_started", scenario=config.scenario, ticks=config.ticks)

        loop = SimTickLoop(
            adapters,
            run_dir=run_dir,
            process=_recording_process(process_func, events),
            proactive_every=config.proactive_every,
            schedule=schedule,
        )
        result = await loop.run(ticks=config.ticks)
        for tick in result.ticks:
            events.write(
                "sim.tick_completed",
                tick=tick.tick,
                persona_messages=tick.persona_messages,
                proactive_jobs=tick.proactive_jobs,
            )
        render_transcript(artifacts.mbox_path, artifacts.transcript_path)
        events.write(
            "sim.run_completed",
            persona_messages=result.persona_messages,
            proactive_jobs=result.proactive_jobs,
        )
        return artifacts

    def _new_run_dir(self) -> Path:
        stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
        candidate = self.runs_dir / stamp
        suffix = 1
        while candidate.exists():
            candidate = self.runs_dir / f"{stamp}-{suffix}"
            suffix += 1
        return candidate


def _config_payload(config: SimRunConfig, process_mode: str) -> dict[str, Any]:
    payload = asdict(config)
    payload["personas"] = [asdict(persona) for persona in config.personas]
    payload["process_mode"] = process_mode
    return payload


def _mock_process(events: EventsLog):
    async def process(**kwargs: Any) -> None:
        events.write(
            "sim.mock_process_email",
            sender_email=kwargs.get("sender_email"),
            subject=kwargs.get("subject"),
            trace_id=kwargs.get("trace_id"),
        )

    return process


def _recording_process(process, events: EventsLog):
    async def wrapped(**kwargs: Any) -> None:
        events.write(
            "sim.process_email_started",
            sender_email=kwargs.get("sender_email"),
            subject=kwargs.get("subject"),
            trace_id=kwargs.get("trace_id"),
        )
        await process(**kwargs)
        events.write(
            "sim.process_email_completed",
            sender_email=kwargs.get("sender_email"),
            trace_id=kwargs.get("trace_id"),
        )

    return wrapped
