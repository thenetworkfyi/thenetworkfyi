"""Run recording for simulation harness executions."""

from __future__ import annotations

import json
import mailbox
import os
import subprocess
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
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.security.log_redaction import redact_structured_values
from thenetwork.sim.run.loop import ProgressCallable, SimTickLoop
from thenetwork.sim.run.mail import (
    SimMessageMeta,
    _extract_body,
    render_transcript,
)
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.personas.population import SimSchedule
from thenetwork.sim.scoring.scoring import (
    IntroductionRevealAuthorization,
    MailFacts,
    MemoryExpectation,
    OutcomeCheck,
    PersonaPII,
    ResponseQualityThresholds,
    ScenarioOutcome,
    score_memory_expectations,
    score_response_quality,
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
    quality_thresholds: ResponseQualityThresholds = ResponseQualityThresholds()


@dataclass(frozen=True)
class SimRunArtifacts:
    run_dir: Path
    config_path: Path
    mbox_path: Path
    transcript_path: Path
    events_path: Path
    audit_path: Path
    private_dir: Path
    raw_mbox_path: Path
    raw_database_dump_path: Path


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
        private_dir=run_dir / "private",
        raw_mbox_path=run_dir / "private" / "all-mail.mbox",
        raw_database_dump_path=run_dir / "private" / "database.dump",
    )


def prepare_private_artifacts(artifacts: SimRunArtifacts) -> None:
    """Create the owner-only area for raw inputs retained solely for scoring."""
    artifacts.private_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(artifacts.private_dir, 0o700)


def write_redacted_json(path: Path, value: Any) -> None:
    """Write a normal simulation artifact only after fail-closed redaction."""
    path.write_text(
        json.dumps(redact_structured_values(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class EventsLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        redacted_fields = redact_structured_values(fields)
        payload = {"event": event, **redacted_fields}
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
        prepare_private_artifacts(artifacts)
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
            write_redacted_json(
                artifacts.config_path, _config_payload(config, process_mode)
            )
            events.write(
                "sim.run_started", scenario=config.scenario, ticks=config.ticks
            )

            loop = SimTickLoop(
                adapters,
                run_dir=run_dir,
                process=_recording_process(process_func, events),
                proactive_every=config.proactive_every,
                schedule=schedule,
                progress=progress,
                on_delivery=_record_delivered_message(events),
                on_proactive_trigger=_record_proactive_trigger(events),
                mbox_path=artifacts.raw_mbox_path,
            )
            result = await loop.run(ticks=config.ticks)
            for tick in result.ticks:
                events.write(
                    "sim.tick_completed",
                    tick=tick.tick,
                    persona_messages=tick.persona_messages,
                    proactive_jobs=tick.proactive_jobs,
                )
            personas_pii = tuple(
                PersonaPII.from_config(persona) for persona in config.personas
            )
            outcome, outcome_memories, emails_by_id = _assemble_scenario_outcome(
                artifacts,
                memories=memories,
                load_database_state=(
                    process_mode == "real" and config.database_name is not None
                ),
                persona_emails=(persona.email for persona in config.personas),
            )
            tier1 = score_seal_mbox(
                artifacts.raw_mbox_path,
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
            quality = score_response_quality(
                artifacts.raw_mbox_path,
                thresholds=config.quality_thresholds,
            )
            events.write(
                "sim.score.quality",
                passed=quality.passed,
                findings=[asdict(finding) for finding in quality.findings],
            )
            if config.expectations:
                tier2 = score_memory_expectations(
                    outcome_memories,
                    config.expectations,
                    emails_by_id,
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
            from thenetwork.sim.run.mail import publish_redacted_mbox

            publish_redacted_mbox(artifacts.raw_mbox_path, artifacts.mbox_path)
            render_transcript(artifacts.mbox_path, artifacts.transcript_path)
        return artifacts


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_provenance() -> dict[str, Any]:
    """Capture the launching checkout's commit sha and dirty-tree state."""
    root = _project_root()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(status.strip())}


def _config_payload(config: SimRunConfig, process_mode: str) -> dict[str, Any]:
    return {
        "scenario": config.scenario,
        "git": _git_provenance(),
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
        "quality_thresholds": {
            "max_noop_admin_alerts": config.quality_thresholds.max_noop_admin_alerts,
            "max_consent_requests_per_recipient": (
                config.quality_thresholds.max_consent_requests_per_recipient
            ),
            "weak_match_pairs": sorted(
                sorted(pair) for pair in config.quality_thresholds.weak_match_pairs
            ),
        },
    }


def _assemble_scenario_outcome(
    artifacts: SimRunArtifacts,
    *,
    memories: Iterable[Memory],
    load_database_state: bool,
    persona_emails: Iterable[str] = (),
) -> tuple[ScenarioOutcome, tuple[Memory, ...], dict[str, str]]:
    if load_database_state:
        consent_rows, database_memories, memory_counts, emails_by_id = (
            _database_outcome_state()
        )
        outcome_memories = database_memories
    else:
        consent_rows = ()
        outcome_memories = tuple(memories)
        emails_by_id = {}
        memory_counts = _memory_counts(outcome_memories, emails_by_id)
    return (
        ScenarioOutcome(
            consent_rows=consent_rows,
            audit_events=_audit_events(artifacts.audit_path)
            if load_database_state
            else (),
            sender_id_hashes={
                email.lower(): sender_id
                for email in persona_emails
                if (sender_id := optional_sender_identifier(email)) is not None
            },
            mail_facts=_mail_facts(artifacts.raw_mbox_path),
            memory_counts=memory_counts,
        ),
        outcome_memories,
        emails_by_id,
    )


def _database_outcome_state() -> tuple[
    tuple[IntroductionRevealAuthorization, ...],
    tuple[Memory, ...],
    dict[str, int],
    dict[str, str],
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
                    person_a_consented=record.person_a_consented,
                    person_b_consented=record.person_b_consented,
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
    return tuple(consent_rows), memories, memory_counts, emails_by_id


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


def _record_delivered_message(events: EventsLog):
    """Write complete synthetic mail only to a simulation run's event log."""

    def record(message, meta: SimMessageMeta | None) -> None:
        events.write(
            "sim.message_delivered",
            body=_extract_body(message),
            direction=meta.direction if meta is not None else "agent->persona",
            persona=meta.persona if meta is not None else None,
            subject=message.get("Subject", ""),
            tick=meta.tick if meta is not None else None,
            trace_id=meta.trace_id if meta is not None else None,
        )

    return record


def _record_proactive_trigger(events: EventsLog):
    """Record the complete SEAL-safe synthetic trigger for a sim-only job."""

    def record(job: dict[str, Any]) -> None:
        events.write(
            "sim.proactive_job_deferred",
            body=job.get("body"),
            subject=job.get("subject"),
            trace_id=job.get("trace_id"),
        )

    return record


def _recording_process(process, events: EventsLog):
    async def wrapped(**kwargs: Any) -> None:
        trace_id = kwargs.get("trace_id")
        events.write(
            "sim.process_email_started",
            sender_email=kwargs.get("sender_email"),
            subject=kwargs.get("subject"),
            trace_id=trace_id,
        )
        try:
            outcome = await process(**kwargs)
        except Exception as exc:
            # Production has Procrastinate's job-retry loop around this same
            # call (see worker/tasks.py's process_email try/except); the sim
            # invokes the task function directly with no such wrapper. Without
            # this, one bad model response aborts the whole multi-tick run
            # instead of just failing the one turn, unlike production.
            events.write(
                "sim.process_email_failed",
                sender_email=kwargs.get("sender_email"),
                trace_id=trace_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
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
