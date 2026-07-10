"""Run recording for simulation harness executions."""
from __future__ import annotations

import json
import mailbox
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from thenetwork.audit import audit_jsonl_file
from sqlmodel import select

from thenetwork.db.models import IntroductionConsent, Memory, Person
from thenetwork.db.session import get_session
from thenetwork.sim.loop import ProgressCallable, SimTickLoop
from thenetwork.sim.mail import _extract_body, render_transcript
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.population import SimSchedule
from thenetwork.sim.scoring import (
    IntroductionRevealAuthorization,
    MailFacts,
    MemoryExpectation,
    OutcomeCheck,
    PersonaPII,
    ScenarioOutcome,
    score_memory_expectations,
    score_scenario_outcomes,
    score_seal_mbox,
)
from thenetwork.worker.tasks import process_email


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SimRunConfig:
    scenario: str
    ticks: int
    proactive_every: int | None
    personas: tuple[PersonaConfig, ...]
    mock_process: bool = True
    expectations: tuple[MemoryExpectation, ...] = ()
    outcome_checks: tuple[OutcomeCheck, ...] = ()
    llm_personas: bool = False
    database_name: str | None = None


@dataclass(frozen=True)
class SimRunArtifacts:
    run_dir: Path
    config_path: Path
    mbox_path: Path
    transcript_path: Path
    events_path: Path
    audit_path: Path


def create_run_artifacts(
    runs_dir: Path,
    *,
    clock: Clock | None = None,
) -> SimRunArtifacts:
    """Reserve the next timestamped artifact paths for a simulation run."""
    now = clock or (lambda: datetime.now(timezone.utc))
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / stamp
    suffix = 1
    while run_dir.exists():
        run_dir = runs_dir / f"{stamp}-{suffix}"
        suffix += 1
    return SimRunArtifacts(
        run_dir=run_dir,
        config_path=run_dir / "config.json",
        mbox_path=run_dir / "all-mail.mbox",
        transcript_path=run_dir / "transcript.md",
        events_path=run_dir / "events.jsonl",
        audit_path=run_dir / "audit.jsonl",
    )


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
        memories: Iterable[Memory] = (),
        progress: ProgressCallable | None = None,
    ) -> SimRunArtifacts:
        artifacts = create_run_artifacts(self.runs_dir, clock=self.clock)
        run_dir = artifacts.run_dir
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

        audit_log = (
            audit_jsonl_file(artifacts.audit_path)
            if process_mode == "real"
            else nullcontext()
        )
        with audit_log:
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
                progress=progress,
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

            personas_pii = tuple(
                PersonaPII.from_config(persona) for persona in config.personas
            )
            outcome, outcome_memories = _assemble_scenario_outcome(
                artifacts,
                memories=memories,
                load_database_state=(
                    process_mode == "real" and config.database_name is not None
                ),
            )
            tier1 = score_seal_mbox(
                artifacts.mbox_path,
                personas_pii,
                tuple(
                    row for row in outcome.consent_rows if row.status == "introduced"
                ),
            )
            events.write(
                "sim.score.tier1",
                passed=tier1.passed,
                findings=[asdict(finding) for finding in tier1.findings],
            )
            if config.expectations:
                tier2 = score_memory_expectations(
                    outcome_memories,
                    config.expectations,
                )
                events.write(
                    "sim.score.tier2",
                    passed=tier2.passed,
                    findings=[asdict(finding) for finding in tier2.findings],
                )
            outcome_score = score_scenario_outcomes(
                outcome,
                config.outcome_checks,
                real_process=process_mode == "real",
                llm_personas=config.llm_personas,
            )
            events.write(
                "sim.score.outcome",
                passed=outcome_score.passed,
                findings=[asdict(finding) for finding in outcome_score.findings],
            )

            events.write(
                "sim.run_completed",
                persona_messages=result.persona_messages,
                proactive_jobs=result.proactive_jobs,
            )
        return artifacts

def _config_payload(config: SimRunConfig, process_mode: str) -> dict[str, Any]:
    return {
        "scenario": config.scenario,
        "ticks": config.ticks,
        "proactive_every": config.proactive_every,
        "personas": [asdict(persona) for persona in config.personas],
        "mock_process": config.mock_process,
        "expectations": [asdict(expectation) for expectation in config.expectations],
        "outcome_checks": [
            {
                "description": check.description,
                "requires_real_process": check.requires_real_process,
                "requires_llm_personas": check.requires_llm_personas,
            }
            for check in config.outcome_checks
        ],
        "llm_personas": config.llm_personas,
        "database_name": config.database_name,
        "process_mode": process_mode,
    }


def _assemble_scenario_outcome(
    artifacts: SimRunArtifacts,
    *,
    memories: Iterable[Memory],
    load_database_state: bool,
) -> tuple[ScenarioOutcome, tuple[Memory, ...]]:
    if load_database_state:
        consent_rows, database_memories, memory_counts = _database_outcome_state()
        outcome_memories = database_memories
    else:
        consent_rows = ()
        outcome_memories = tuple(memories)
        memory_counts = _memory_counts(outcome_memories, {})
    return (
        ScenarioOutcome(
            consent_rows=consent_rows,
            audit_events=_audit_events(artifacts.audit_path)
            if load_database_state
            else (),
            mail_facts=_mail_facts(artifacts.mbox_path),
            memory_counts=memory_counts,
        ),
        outcome_memories,
    )


def _database_outcome_state() -> tuple[
    tuple[IntroductionRevealAuthorization, ...], tuple[Memory, ...], dict[str, int]
]:
    consent_rows = []
    with get_session() as session:
        records = session.exec(select(IntroductionConsent)).all()
        for record in records:
            person_a = session.get(Person, record.person_a_id)
            person_b = session.get(Person, record.person_b_id)
            if person_a is None or person_b is None:
                continue
            consent_rows.append(
                IntroductionRevealAuthorization(
                    person_a_email=person_a.email,
                    person_b_email=person_b.email,
                    status=record.status,
                )
            )
        memories = tuple(
            Memory(
                id=memory.id,
                text=memory.text,
                refs=list(memory.refs or ()),
                gist=memory.gist,
            )
            for memory in session.exec(select(Memory)).all()
        )
        emails_by_id = {
            person.id: person.email for person in session.exec(select(Person)).all()
        }
        memory_counts = _memory_counts(memories, emails_by_id)
    return tuple(consent_rows), memories, memory_counts


def _audit_events(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _mail_facts(path: Path) -> tuple[MailFacts, ...]:
    box = mailbox.mbox(path)
    try:
        return tuple(
            MailFacts(
                sender=message.get("From", ""),
                recipients=frozenset(
                    address.lower()
                    for _display_name, address in getaddresses(
                        message.get_all("To", []) + message.get_all("Cc", [])
                    )
                    if address
                ),
                subject=message.get("Subject", ""),
                body=_extract_body(message),
            )
            for message in box
        )
    finally:
        box.close()


def _memory_counts(
    memories: Iterable[Memory], emails_by_id: dict[str, str]
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for memory in memories:
        for ref in memory.refs or ():
            email = emails_by_id.get(ref, ref if "@" in ref else None)
            if email is not None:
                counts[email] += 1
    return dict(counts)


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
        trace_id = kwargs.get("trace_id")
        events.write(
            "sim.process_email_started",
            sender_email=kwargs.get("sender_email"),
            subject=kwargs.get("subject"),
            trace_id=trace_id,
        )
        outcome = await process(**kwargs)
        events.write(
            "sim.process_email_completed",
            sender_email=kwargs.get("sender_email"),
            trace_id=trace_id,
        )
        _record_outcome(events, outcome, trace_id=trace_id)

    return wrapped


def _record_outcome(events: EventsLog, outcome: Any, *, trace_id: str | None) -> None:
    """Translate an optional structured process outcome into scorable events.

    A scripted `process` may return a dict with `tool_calls`, `total_tokens`,
    `cost_usd`, and `judge_score` so scenario tests can produce comparable
    metrics without a live agent/model run. Matches the real audit trail's
    `agent.tool.completed` naming (see thenetwork/audit.py) for consistency.
    total_tokens/cost_usd describe the outcome as a whole, so they are written
    exactly once per outcome (a separate event from the per-tool-call ones),
    regardless of how many tool_calls the outcome lists.
    """
    if not isinstance(outcome, dict):
        return
    for tool_name in outcome.get("tool_calls", ()):
        events.write("agent.tool.completed", tool_name=tool_name, trace_id=trace_id)
    total_tokens = outcome.get("total_tokens")
    cost_usd = outcome.get("cost_usd")
    if total_tokens is not None or cost_usd is not None:
        events.write(
            "sim.process_outcome_metrics",
            total_tokens=total_tokens or 0,
            cost_usd=cost_usd or 0.0,
            trace_id=trace_id,
        )
    judge_score = outcome.get("judge_score")
    if judge_score is not None:
        events.write("sim.judge.transcript", score=judge_score, trace_id=trace_id)
