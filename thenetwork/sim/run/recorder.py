"""Run recording for simulation harness executions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mailbox
import os
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path
from time import perf_counter
from typing import Any

from procrastinate import utils as procrastinate_utils
from thenetwork.audit import audit_jsonl_file
from sqlmodel import select

from thenetwork.agent.prompts import SYSTEM_PROMPT
from thenetwork.db.models import (
    Event,
    EventRecommendation,
    IntroductionConsent,
    Memory,
    Person,
)
from thenetwork.db.session import get_session
from thenetwork.security.log_redaction import redact_structured_values
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.settings import get_settings
from thenetwork.sim.personas.llm_persona import _PERSONA_PROMPT
from thenetwork.sim.run.loop import ProgressCallable, SimTickLoop
from thenetwork.sim.run.mail import (
    SimMessageMeta,
    _extract_body,
    render_transcript,
)
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.personas.population import SimSchedule
from thenetwork.sim.scoring.scoring import (
    EventOutcomeFact,
    EventRecommendationOutcomeFact,
    IntroductionConsentState,
    MailFacts,
    MemoryExpectation,
    OutcomeCheck,
    PersonaPII,
    ProactiveEventTriggerOutcomeFact,
    ResponseQualityThresholds,
    ScenarioOutcome,
    score_memory_expectations,
    score_presentation_mbox,
    score_response_quality,
    score_scenario_outcomes,
    score_seal_mbox,
)
from thenetwork.worker.tasks import app, process_email


Clock = Callable[[], datetime]
_SIM_PROCESS_EMAIL_QUEUE = "simulation_process_email"


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
        json.dumps(_redact_public_values(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _redact_public_values(value: Any) -> Any:
    """Redact values and remove markup, which is never a public artifact format."""
    redacted = _omit_markup(redact_structured_values(value))
    if isinstance(value, dict) and isinstance(redacted, dict):
        _restore_public_model_identifiers(value, redacted)
        _restore_static_prompt_hashes(value, redacted)
    return redacted


def _restore_public_model_identifiers(
    source: dict[str, Any], redacted: dict[str, Any]
) -> None:
    """Keep strict model identifiers readable despite PII recognizer false positives."""
    source_provenance = source.get("runtime_provenance")
    redacted_provenance = redacted.get("runtime_provenance")
    if not isinstance(source_provenance, dict) or not isinstance(
        redacted_provenance, dict
    ):
        return
    source_models = source_provenance.get("models")
    redacted_models = redacted_provenance.get("models")
    if not isinstance(source_models, dict) or not isinstance(redacted_models, dict):
        return
    for role in ("agent", "persona", "sanitizer", "embedding"):
        source_model = source_models.get(role)
        redacted_model = redacted_models.get(role)
        if not isinstance(source_model, dict) or not isinstance(redacted_model, dict):
            continue
        identifier = source_model.get("identifier")
        if _is_public_model_identifier(identifier):
            redacted_model["identifier"] = identifier
        else:
            redacted_model["identifier"] = "[redacted]"


def _restore_static_prompt_hashes(
    source: dict[str, Any], redacted: dict[str, Any]
) -> None:
    """Keep prompt provenance digests intact despite PII recognizer false positives.

    A SHA-256 hex digest is fixed-shape, content-free, and one-way, but its
    random hex can incidentally be labelled by the span classifier (an
    identifier-shaped run of letters and digits), which silently corrupts the
    provenance anchor tying a run to the prompt text that produced it. Only a
    value that is exactly 64 lowercase hex characters is restored, so nothing
    but a digest can survive this path.
    """
    source_provenance = source.get("runtime_provenance")
    redacted_provenance = redacted.get("runtime_provenance")
    if not isinstance(source_provenance, dict) or not isinstance(
        redacted_provenance, dict
    ):
        return
    source_hashes = source_provenance.get("static_prompt_sha256")
    redacted_hashes = redacted_provenance.get("static_prompt_sha256")
    if not isinstance(source_hashes, dict) or not isinstance(redacted_hashes, dict):
        return
    for role in redacted_hashes:
        digest = source_hashes.get(role)
        redacted_hashes[role] = digest if _is_sha256_digest(digest) else "[redacted]"


def _is_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_public_model_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 200:
        return False
    if not value.isascii() or not all(
        character.isalnum() or character in "._:/+-" for character in value
    ):
        return False
    lowered = value.lower()
    return not lowered.startswith(("sk-", "api_key", "token", "secret", "password"))


def _omit_markup(value: Any) -> Any:
    if isinstance(value, str):
        return "[markup-omitted]" if "<" in value and ">" in value else value
    if isinstance(value, dict):
        return {key: _omit_markup(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_omit_markup(item) for item in value]
    return value


class EventsLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        redacted_fields = _redact_public_values(fields)
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
            drain_jobs = None
        elif config.mock_process is False:
            process_mode = "real"
            job_drainer = _SimulationJobDrainer(
                events, concurrency=get_settings().worker_concurrency
            )
            process_func = _recording_deferred_process(events, job_drainer)
            drain_jobs = job_drainer
        else:
            process_mode = "mock"
            process_func = _mock_process(events)
            drain_jobs = None

        audit_log = (
            audit_jsonl_file(
                artifacts.audit_path,
                include_model_responses=False,
            )
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
                process=(
                    process_func
                    if process_mode == "real"
                    else _recording_process(process_func, events)
                ),
                proactive_every=config.proactive_every,
                schedule=schedule,
                progress=progress,
                on_delivery=_record_delivered_message(events),
                on_proactive_trigger=_record_proactive_trigger(events),
                on_stage_timing=_record_stage_timing(events),
                drain_jobs=drain_jobs,
                mbox_path=artifacts.raw_mbox_path,
            )
            if process_mode == "real":
                async with app.open_async():
                    result = await loop.run(ticks=config.ticks)
            else:
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
            )
            events.write(
                "sim.score.tier1",
                passed=tier1.passed,
                findings=[asdict(finding) for finding in tier1.findings],
            )
            presentation = score_presentation_mbox(
                artifacts.raw_mbox_path,
                (persona.email for persona in config.personas),
            )
            events.write(
                "sim.score.presentation",
                passed=presentation.passed,
                findings=[asdict(finding) for finding in presentation.findings],
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
                    outcome.mail_facts,
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
        "runtime_provenance": _runtime_provenance(config, process_mode),
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


def _runtime_provenance(config: SimRunConfig, process_mode: str) -> dict[str, Any]:
    """Return versioned, public-safe settings and static prompt fingerprints."""
    settings = get_settings()
    real_process = process_mode == "real"
    return {
        "version": 1,
        "models": {
            "agent": {
                "identifier": settings.agent_model,
                "active": real_process,
            },
            "persona": {
                "identifier": settings.small_agent_model,
                "active": config.llm_personas,
            },
            "sanitizer": {
                "identifier": settings.sanitize_model,
                "active": real_process,
            },
            "embedding": {
                "identifier": settings.embed_model,
                "active": real_process,
            },
        },
        "settings": {
            "agent_thinking_level": (
                str(settings.agent_thinking_level)
                if settings.agent_thinking_level is not None
                else None
            ),
            "agent_request_limit": settings.agent_request_limit,
            "agent_total_tokens_limit": settings.agent_total_tokens_limit,
            "model_request_timeout_seconds": settings.model_request_timeout_seconds,
            "sanitizer_mode": "privacy-filter",
        },
        "static_prompt_sha256": {
            "agent": _sha256_text(SYSTEM_PROMPT),
            "persona_template": _sha256_text(_PERSONA_PROMPT),
        },
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assemble_scenario_outcome(
    artifacts: SimRunArtifacts,
    *,
    memories: Iterable[Memory],
    load_database_state: bool,
    persona_emails: Iterable[str] = (),
) -> tuple[ScenarioOutcome, tuple[Memory, ...], dict[str, str]]:
    if load_database_state:
        (
            consent_rows,
            database_memories,
            memory_counts,
            emails_by_id,
            event_rows,
            event_recommendation_rows,
        ) = _database_outcome_state()
        outcome_memories = database_memories
    else:
        consent_rows = ()
        outcome_memories = tuple(memories)
        emails_by_id = {}
        memory_counts = _memory_counts(outcome_memories, emails_by_id)
        event_rows = ()
        event_recommendation_rows = ()
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
            event_rows=event_rows,
            event_recommendation_rows=event_recommendation_rows,
            proactive_event_triggers=_proactive_event_triggers(artifacts.events_path),
        ),
        outcome_memories,
        emails_by_id,
    )


def _database_outcome_state() -> tuple[
    tuple[IntroductionConsentState, ...],
    tuple[Memory, ...],
    dict[str, int],
    dict[str, str],
    tuple[EventOutcomeFact, ...],
    tuple[EventRecommendationOutcomeFact, ...],
]:
    consent_rows = []
    with get_session() as session:
        people = tuple(session.exec(select(Person)).all())
        emails_by_id = {person.id: person.email for person in people}
        sender_id_hashes_by_id = {
            person_id: optional_sender_identifier(email)
            for person_id, email in emails_by_id.items()
        }
        records = session.exec(select(IntroductionConsent)).all()
        for record in records:
            person_a_email = emails_by_id.get(record.person_a_id)
            person_b_email = emails_by_id.get(record.person_b_id)
            if person_a_email is None or person_b_email is None:
                continue
            consent_rows.append(
                IntroductionConsentState(
                    person_a_email=person_a_email,
                    person_b_email=person_b_email,
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
        memory_counts = _memory_counts(memories, emails_by_id)
        now = datetime.now(timezone.utc)
        event_rows = tuple(
            EventOutcomeFact(
                event_key=_event_correlation_key(event_id),
                owner_sender_id_hash=sender_id_hashes_by_id.get(submitter_id),
                version=version,
                active=cancelled_at is None and _as_utc(expires_at) > now,
                recurring=recurring,
            )
            for (
                event_id,
                submitter_id,
                version,
                expires_at,
                cancelled_at,
                recurring,
            ) in session.exec(
                select(
                    Event.id,
                    Event.submitter_id,
                    Event.version,
                    Event.expires_at,
                    Event.cancelled_at,
                    Event.recurrence.is_not(None),
                )
            ).all()
        )
        event_recommendation_rows = tuple(
            EventRecommendationOutcomeFact(
                event_key=_event_correlation_key(event_id),
                recipient_sender_id_hash=sender_id_hashes_by_id.get(person_id),
                event_version=event_version,
                notified=notified_at is not None,
            )
            for (
                event_id,
                person_id,
                event_version,
                notified_at,
            ) in session.exec(
                select(
                    EventRecommendation.event_id,
                    EventRecommendation.person_id,
                    EventRecommendation.event_version,
                    EventRecommendation.notified_at,
                )
            ).all()
        )
    return (
        tuple(consent_rows),
        memories,
        memory_counts,
        emails_by_id,
        event_rows,
        event_recommendation_rows,
    )


def _event_correlation_key(event_id: str) -> str:
    """Derive a stable public key from a server-generated opaque event id."""
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
    return f"evt_v1_{digest}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _proactive_event_triggers(
    path: Path,
) -> tuple[ProactiveEventTriggerOutcomeFact, ...]:
    if not path.exists():
        return ()
    triggers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if (
            event.get("event") != "sim.proactive_job_deferred"
            or event.get("trigger_kind") != "event"
            or not isinstance(event.get("event_key"), str)
            or not isinstance(event.get("event_version"), int)
        ):
            continue
        recipient_sender_id_hash = event.get("recipient_sender_id_hash")
        triggers.append(
            ProactiveEventTriggerOutcomeFact(
                event_key=event["event_key"],
                recipient_sender_id_hash=(
                    recipient_sender_id_hash
                    if isinstance(recipient_sender_id_hash, str)
                    else None
                ),
                event_version=event["event_version"],
            )
        )
    return tuple(triggers)


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
    """Record delivery metadata without copying raw mail into public events."""

    def record(message, meta: SimMessageMeta | None) -> None:
        body = _extract_body(message)
        events.write(
            "sim.message_delivered",
            body_chars=len(body),
            direction=meta.direction if meta is not None else "agent->persona",
            persona=meta.persona if meta is not None else None,
            subject=message.get("Subject", ""),
            tick=meta.tick if meta is not None else None,
            trace_id=meta.trace_id if meta is not None else None,
        )

    return record


def _record_proactive_trigger(events: EventsLog):
    """Record safe trigger metadata; event bodies remain model-context only."""

    def record(job: dict[str, Any]) -> None:
        event_id = job.get("proactive_event_id")
        event_version = job.get("proactive_event_version")
        if isinstance(event_id, str) and isinstance(event_version, int):
            sender_email = job.get("sender_email")
            events.write(
                "sim.proactive_job_deferred",
                event_key=_event_correlation_key(event_id),
                event_version=event_version,
                recipient_sender_id_hash=(
                    optional_sender_identifier(sender_email)
                    if isinstance(sender_email, str)
                    else None
                ),
                subject=job.get("subject"),
                trace_id=job.get("trace_id"),
                trigger_kind="event",
            )
            return
        events.write(
            "sim.proactive_job_deferred",
            body=job.get("body"),
            subject=job.get("subject"),
            trace_id=job.get("trace_id"),
            trigger_kind="people",
        )

    return record


def _record_stage_timing(events: EventsLog):
    """Record elapsed wall-clock time for a simulation stage."""

    def record(event: str, **fields: Any) -> None:
        events.write(event, **fields)

    return record


async def _defer_process_email(**kwargs: Any) -> int:
    """Queue real simulation mail through the production task definition."""
    return await process_email.configure(queue=_SIM_PROCESS_EMAIL_QUEUE).defer_async(
        **kwargs
    )


@dataclass
class _SimulationJobDrainer:
    """Run and observe simulation jobs until each one reaches a terminal state.

    The task's own RetryStrategy decides whether and when an exception is
    retried. If it schedules a future retry, wait for that schedule rather than
    allowing scoring or disposable-database cleanup to drop the pending turn.
    """

    events: EventsLog
    concurrency: int = 1
    job_ids: set[int] = field(default_factory=set)
    job_started_at: dict[int, float] = field(default_factory=dict)
    _reported_terminal_job_ids: set[int] = field(default_factory=set)
    _reported_retry_attempts: set[tuple[int, int]] = field(default_factory=set)

    async def __call__(self) -> None:
        if not self.job_ids:
            return

        ran_worker = False
        while True:
            jobs = tuple(
                job
                for job in await app.job_manager.list_jobs_async(
                    queue=_SIM_PROCESS_EMAIL_QUEUE
                )
                if job.id in self.job_ids
            )
            pending = tuple(job for job in jobs if job.status in {"todo", "doing"})
            self._record_job_outcomes(jobs)
            self._record_retries(pending)
            if not pending:
                return
            if any(job.status == "doing" for job in pending):
                raise RuntimeError("simulation worker stopped with a job still running")

            retry_times = tuple(job.scheduled_at for job in pending if job.scheduled_at)
            if len(retry_times) == len(pending):
                wait_seconds = max(
                    0.0,
                    (min(retry_times) - procrastinate_utils.utcnow()).total_seconds(),
                )
                if wait_seconds:
                    await _sleep_until_retry(wait_seconds)
                    continue
            elif ran_worker:
                raise RuntimeError("simulation worker left a ready job unprocessed")

            await app.run_worker_async(
                queues=[_SIM_PROCESS_EMAIL_QUEUE],
                concurrency=self.concurrency,
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
            )
            ran_worker = True

    def _record_job_outcomes(self, jobs: tuple[Any, ...]) -> None:
        for job in jobs:
            if (
                job.id is None
                or job.id in self._reported_terminal_job_ids
                or job.status in {"todo", "doing"}
            ):
                continue
            self._reported_terminal_job_ids.add(job.id)
            self._record_retry_attempts(job, retry_count=max(0, job.attempts - 1))
            fields = {
                "sender_email": job.task_kwargs.get("sender_email"),
                "trace_id": job.task_kwargs.get("trace_id"),
                "attempts": job.attempts,
                "job_status": job.status,
            }
            started_at = self.job_started_at.pop(job.id, None)
            if started_at is not None:
                fields["elapsed_ms"] = round((perf_counter() - started_at) * 1000)
            self.events.write(
                (
                    "sim.process_email_completed"
                    if job.status == "succeeded"
                    else "sim.process_email_failed"
                ),
                **fields,
            )

    def _record_retries(self, jobs: tuple[Any, ...]) -> None:
        for job in jobs:
            if job.id is None or job.attempts < 1:
                continue
            self._record_retry_attempts(job, retry_count=job.attempts)

    def _record_retry_attempts(self, job: Any, *, retry_count: int) -> None:
        """Record every scheduled retry, including ones completed within a drain."""
        for attempt in range(1, retry_count + 1):
            retry = (job.id, attempt)
            if retry in self._reported_retry_attempts:
                continue
            self._reported_retry_attempts.add(retry)
            self.events.write(
                "sim.process_email_retrying",
                sender_email=job.task_kwargs.get("sender_email"),
                trace_id=job.task_kwargs.get("trace_id"),
                attempt=attempt,
            )


async def _sleep_until_retry(seconds: float) -> None:
    """Wait until Procrastinate's next scheduled simulation retry is due."""
    await asyncio.sleep(seconds)


def _recording_deferred_process(
    events: EventsLog,
    drainer: _SimulationJobDrainer,
):
    """Record inbound queueing without claiming enqueue means completion."""

    async def wrapped(**kwargs: Any) -> None:
        trace_id = kwargs.get("trace_id")
        events.write(
            "sim.process_email_started",
            sender_email=kwargs.get("sender_email"),
            subject=kwargs.get("subject"),
            trace_id=trace_id,
        )
        started_at = perf_counter()
        try:
            job_id = await _defer_process_email(**kwargs)
        except Exception as exc:
            events.write(
                "sim.process_email_failed",
                sender_email=kwargs.get("sender_email"),
                trace_id=trace_id,
                error_type=type(exc).__name__,
                error=str(exc),
                elapsed_ms=round((perf_counter() - started_at) * 1000),
            )
        else:
            drainer.job_ids.add(job_id)
            drainer.job_started_at[job_id] = started_at
            events.write(
                "sim.process_email_enqueued",
                trace_id=trace_id,
                elapsed_ms=round((perf_counter() - started_at) * 1000),
            )

    return wrapped


def _recording_process(process, events: EventsLog):
    async def wrapped(**kwargs: Any) -> None:
        trace_id = kwargs.get("trace_id")
        events.write(
            "sim.process_email_started",
            sender_email=kwargs.get("sender_email"),
            subject=kwargs.get("subject"),
            trace_id=trace_id,
        )
        started_at = perf_counter()
        try:
            outcome = await process(**kwargs)
        except Exception as exc:
            events.write(
                "sim.process_email_failed",
                sender_email=kwargs.get("sender_email"),
                trace_id=trace_id,
                error_type=type(exc).__name__,
                error=str(exc),
                elapsed_ms=round((perf_counter() - started_at) * 1000),
            )
            return
        events.write(
            "sim.process_email_completed",
            sender_email=kwargs.get("sender_email"),
            trace_id=trace_id,
            elapsed_ms=round((perf_counter() - started_at) * 1000),
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
