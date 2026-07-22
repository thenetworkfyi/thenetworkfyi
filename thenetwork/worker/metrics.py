"""Best-effort outbound worker state metrics.

The worker never opens a metrics listener. A background OpenTelemetry metric
reader exports four unlabelled gauges to the Collector over OTLP/HTTP. Metric
callbacks contain database and exporter failures so observability cannot alter
email, queue, or intake behavior.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from math import ceil
from threading import Lock
from time import monotonic, time
from typing import Callable, Iterable

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from thenetwork.audit import audit_warning_event
from thenetwork.db.models import PrimaryIntakeState
from thenetwork.settings import get_settings

PRODUCER_LAST_SUCCESS_METRIC = "thenetwork_producer_last_success_timestamp_seconds"
JOB_QUEUE_DEPTH_METRIC = "thenetwork_job_queue_depth"
OLDEST_PENDING_JOB_AGE_METRIC = "thenetwork_oldest_pending_job_age_seconds"
PRIMARY_INTAKE_PAUSED_METRIC = "thenetwork_primary_intake_paused"
CONTROL_ACTIONS_METRIC = "thenetwork_control_actions_total"
AGENT_USAGE_LIMIT_EXCEEDED_METRIC = "thenetwork_agent_usage_limit_exceeded_total"
JOBS_EXHAUSTED_METRIC = "thenetwork_jobs_exhausted_total"


class ControlAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    BAN = "ban"
    UNBAN = "unban"


class ControlActor(StrEnum):
    ADMIN = "admin"
    SYSTEM = "system"


class ControlReason(StrEnum):
    ADMIN = "admin"
    NEW_SENDER_BURST = "new_sender_burst"
    COORDINATED_ABUSE = "coordinated_abuse"
    AUTOMATIC_POLICY = "automatic_policy"


_PRIMARY_INTAKE_PAUSE_REASONS = frozenset(
    {
        ControlReason.ADMIN.value,
        ControlReason.NEW_SENDER_BURST.value,
        ControlReason.COORDINATED_ABUSE.value,
    }
)

_PENDING_JOB_TIMINGS = text(
    """
    SELECT
        jobs.scheduled_at,
        MIN(events.at) FILTER (WHERE events.type = 'deferred') AS enqueued_at
    FROM procrastinate_jobs AS jobs
    LEFT JOIN procrastinate_events AS events ON events.job_id = jobs.id
    WHERE jobs.status = 'todo'::procrastinate_job_status
      AND (jobs.scheduled_at IS NULL OR jobs.scheduled_at <= :now)
    GROUP BY jobs.id, jobs.scheduled_at
    """
)


@dataclass(frozen=True, slots=True)
class PendingJobTiming:
    scheduled_at: datetime | None
    enqueued_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkerStateSnapshot:
    queue_depth: int
    oldest_pending_job_age_seconds: float
    primary_intake_paused: int
    primary_intake_pause_reason: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _metrics_session():
    """Use an isolated short-lived connection, never the worker's pool."""
    settings = get_settings()
    timeout_seconds = max(1, ceil(settings.worker_metrics_collection_timeout_seconds))
    engine = create_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": timeout_seconds,
            "options": f"-c statement_timeout={timeout_seconds * 1000}",
        },
    )
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def summarize_pending_jobs(
    jobs: Iterable[PendingJobTiming], *, now: datetime
) -> tuple[int, float]:
    """Return due/runnable job count and the oldest due age.

    A future ``scheduled_at`` is not backlog. For a due scheduled job, age
    starts when it became due; for an immediately runnable job, age starts at
    its initial Procrastinate ``deferred`` event. Missing event metadata is
    counted in depth but contributes a zero age.
    """
    ages: list[float] = []
    depth = 0
    for job in jobs:
        if job.scheduled_at is not None and job.scheduled_at > now:
            continue
        depth += 1
        runnable_since = job.scheduled_at or job.enqueued_at
        age = (now - runnable_since).total_seconds() if runnable_since else 0.0
        ages.append(max(0.0, age))
    return depth, max(ages, default=0.0)


def collect_worker_state(
    *, session_factory=None, now: datetime | None = None
) -> WorkerStateSnapshot:
    """Read only non-identifying aggregate state from Postgres."""
    collected_at = now or _utcnow()
    factory = session_factory or _metrics_session
    with factory() as session:
        rows = session.execute(_PENDING_JOB_TIMINGS, {"now": collected_at}).mappings()
        jobs = [
            PendingJobTiming(
                scheduled_at=row["scheduled_at"],
                enqueued_at=row["enqueued_at"],
            )
            for row in rows
        ]
        intake = session.get(PrimaryIntakeState, "primary")

    depth, oldest_age = summarize_pending_jobs(jobs, now=collected_at)
    intake_paused = bool(intake and intake.paused)
    pause_reason = "none"
    if intake_paused:
        candidate = getattr(intake, "pause_reason", None)
        pause_reason = (
            candidate if candidate in _PRIMARY_INTAKE_PAUSE_REASONS else "unknown"
        )
    return WorkerStateSnapshot(
        queue_depth=depth,
        oldest_pending_job_age_seconds=oldest_age,
        primary_intake_paused=int(intake_paused),
        primary_intake_pause_reason=pause_reason,
    )


_producer_lock = Lock()
_producer_last_success_timestamp_seconds = 0.0


def record_producer_poll_success(*, timestamp: float | None = None) -> None:
    """Record a completed poll without performing I/O or raising."""
    global _producer_last_success_timestamp_seconds
    try:
        completed_at = time() if timestamp is None else float(timestamp)
        with _producer_lock:
            _producer_last_success_timestamp_seconds = completed_at
    except Exception:
        # Metrics must never change producer completion behavior.
        return


def producer_last_success_timestamp_seconds() -> float:
    with _producer_lock:
        return _producer_last_success_timestamp_seconds


class _StateObserver:
    """Share one contained database read across three gauge callbacks."""

    def __init__(
        self,
        collector: Callable[[], WorkerStateSnapshot] = collect_worker_state,
        *,
        cache_seconds: float = 1.0,
    ) -> None:
        self._collector = collector
        self._cache_seconds = cache_seconds
        self._lock = Lock()
        self._refresh_after = 0.0
        self._snapshot: WorkerStateSnapshot | None = None

    def snapshot(self) -> WorkerStateSnapshot | None:
        current = monotonic()
        with self._lock:
            if current < self._refresh_after:
                return self._snapshot
            try:
                self._snapshot = self._collector()
            except Exception as exc:
                self._snapshot = None
                try:
                    audit_warning_event(
                        "worker.metrics_collection_failed",
                        error_type=type(exc).__name__,
                    )
                except Exception:
                    pass
            self._refresh_after = current + self._cache_seconds
            return self._snapshot


_provider_lock = Lock()
_meter_provider: MeterProvider | None = None
_instruments: list[object] = []
_control_actions_counter: object | None = None
_agent_usage_limit_exceeded_counter: object | None = None
_jobs_exhausted_counter: object | None = None


def _add_counter(counter: object | None, *, attributes: dict[str, str] | None) -> None:
    """Increment one configured counter without affecting application behavior."""
    if counter is None:
        return
    try:
        counter.add(1, attributes=attributes)
    except Exception:
        return


def record_control_action(
    *, action: ControlAction, actor: ControlActor, reason: ControlReason
) -> None:
    """Record one committed control transition with closed dimensions."""
    try:
        attributes = {
            "action": ControlAction(action).value,
            "actor": ControlActor(actor).value,
            "reason": ControlReason(reason).value,
        }
    except (TypeError, ValueError):
        return
    _add_counter(_control_actions_counter, attributes=attributes)


def record_agent_usage_limit_exceeded() -> None:
    """Record one agent run interrupted by its configured usage limit."""
    _add_counter(_agent_usage_limit_exceeded_counter, attributes=None)


def record_job_exhausted() -> None:
    """Record one process_email job reaching its final failed attempt."""
    _add_counter(_jobs_exhausted_counter, attributes=None)


def _state_callback(
    observer: _StateObserver, attribute: str
) -> Callable[[object], list[Observation]]:
    def callback(_options: object) -> list[Observation]:
        snapshot = observer.snapshot()
        if snapshot is None:
            return []
        return [Observation(getattr(snapshot, attribute))]

    return callback


def configure_worker_metrics(
    *,
    exporter_factory=OTLPMetricExporter,
    reader_factory=PeriodicExportingMetricReader,
    provider_factory=MeterProvider,
    state_observer: _StateObserver | None = None,
) -> MeterProvider | None:
    """Start background OTLP export, returning ``None`` on setup failure."""
    global _agent_usage_limit_exceeded_counter
    global _control_actions_counter, _jobs_exhausted_counter
    global _meter_provider, _instruments
    with _provider_lock:
        if _meter_provider is not None:
            return _meter_provider
        try:
            settings = get_settings()
            exporter = exporter_factory(
                endpoint=settings.worker_metrics_otlp_endpoint,
                timeout=settings.worker_metrics_export_timeout_seconds,
            )
            reader = reader_factory(
                exporter,
                export_interval_millis=(
                    settings.worker_metrics_export_interval_seconds * 1000
                ),
                export_timeout_millis=(
                    settings.worker_metrics_export_timeout_seconds * 1000
                ),
            )
            provider = provider_factory(metric_readers=[reader])
            meter = provider.get_meter("thenetwork.worker")
            observer = state_observer or _StateObserver()
            instruments = [
                meter.create_observable_gauge(
                    PRODUCER_LAST_SUCCESS_METRIC,
                    callbacks=[
                        lambda _options: [
                            Observation(producer_last_success_timestamp_seconds())
                        ]
                    ],
                    unit="s",
                    description="Unix timestamp of the last completed IMAP poll.",
                ),
                meter.create_observable_gauge(
                    JOB_QUEUE_DEPTH_METRIC,
                    callbacks=[_state_callback(observer, "queue_depth")],
                    unit="1",
                    description="Runnable or overdue Procrastinate jobs.",
                ),
                meter.create_observable_gauge(
                    OLDEST_PENDING_JOB_AGE_METRIC,
                    callbacks=[
                        _state_callback(observer, "oldest_pending_job_age_seconds")
                    ],
                    unit="s",
                    description="Age of the oldest runnable or overdue job.",
                ),
                meter.create_observable_gauge(
                    PRIMARY_INTAKE_PAUSED_METRIC,
                    callbacks=[
                        lambda _options: (
                            [
                                Observation(
                                    snapshot.primary_intake_paused,
                                    attributes={
                                        "reason": snapshot.primary_intake_pause_reason
                                    },
                                )
                            ]
                            if (snapshot := observer.snapshot()) is not None
                            else []
                        )
                    ],
                    unit="1",
                    description="Whether durable primary intake state is paused.",
                ),
            ]
            control_actions_counter = meter.create_counter(
                CONTROL_ACTIONS_METRIC,
                unit="1",
                description="Committed operator and automated control transitions.",
            )
            agent_usage_limit_exceeded_counter = meter.create_counter(
                AGENT_USAGE_LIMIT_EXCEEDED_METRIC,
                unit="1",
                description="Agent runs interrupted by configured usage limits.",
            )
            jobs_exhausted_counter = meter.create_counter(
                JOBS_EXHAUSTED_METRIC,
                unit="1",
                description="Process-email jobs that exhausted all retry attempts.",
            )
            instruments.extend(
                [
                    control_actions_counter,
                    agent_usage_limit_exceeded_counter,
                    jobs_exhausted_counter,
                ]
            )
            _instruments = instruments
            _control_actions_counter = control_actions_counter
            _agent_usage_limit_exceeded_counter = agent_usage_limit_exceeded_counter
            _jobs_exhausted_counter = jobs_exhausted_counter
            _meter_provider = provider
            return provider
        except Exception as exc:
            try:
                audit_warning_event(
                    "worker.metrics_setup_failed",
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass
            return None
