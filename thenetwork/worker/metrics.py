"""Best-effort outbound worker operational and model-accounting metrics.

The worker never opens a metrics listener. A background OpenTelemetry metric
reader exports state, event, model-accounting, and lifecycle instruments to the
Collector over OTLP/HTTP. Metric callbacks and recorders contain database and
exporter failures so observability cannot alter email, queue, or intake behavior.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from math import ceil
from pathlib import Path
from threading import Lock
from time import monotonic, time
from typing import Callable, Generic, Iterable, TypeVar

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
LLM_REQUESTS_METRIC = "thenetwork_llm_requests_total"
LLM_TOKENS_METRIC = "thenetwork_llm_tokens_total"
LLM_ESTIMATED_COST_METRIC = "thenetwork_llm_estimated_cost_usd_total"
LLM_REQUEST_DURATION_METRIC = "thenetwork_llm_request_duration_seconds"
EMAIL_LIFECYCLE_DURATION_METRIC = "thenetwork_email_lifecycle_duration_seconds"
EMAIL_QUEUE_DURATION_METRIC = "thenetwork_email_queue_duration_seconds"
AGENT_RUN_DURATION_METRIC = "thenetwork_agent_run_duration_seconds"
PEOPLE_TOTAL_METRIC = "thenetwork_people_total"
ACTIVATED_PEOPLE_TOTAL_METRIC = "thenetwork_activated_people_total"
ACTIVE_SENDERS_WEEKLY_METRIC = "thenetwork_active_senders_weekly"
NETWORK_DENSITY_METRIC = "thenetwork_network_density"
WORKER_PROCESS_RESIDENT_MEMORY_METRIC = (
    "thenetwork_worker_process_resident_memory_bytes"
)
WORKER_PROCESS_CPU_SECONDS_METRIC = "thenetwork_worker_process_cpu_seconds"
WORKER_PROCESS_OPEN_FDS_METRIC = "thenetwork_worker_process_open_fds"
WORKER_PROCESS_THREADS_METRIC = "thenetwork_worker_process_threads"
WORKER_CGROUP_MEMORY_CURRENT_METRIC = "thenetwork_worker_cgroup_memory_current_bytes"
WORKER_CGROUP_MEMORY_MAX_METRIC = "thenetwork_worker_cgroup_memory_max_bytes"
WORKER_CGROUP_MEMORY_PEAK_METRIC = "thenetwork_worker_cgroup_memory_peak_bytes"
WORKER_CGROUP_CPU_PERIODS_METRIC = "thenetwork_worker_cgroup_cpu_periods_total"
WORKER_CGROUP_CPU_THROTTLED_PERIODS_METRIC = (
    "thenetwork_worker_cgroup_cpu_throttled_periods_total"
)
WORKER_CGROUP_CPU_THROTTLED_SECONDS_METRIC = (
    "thenetwork_worker_cgroup_cpu_throttled_seconds_total"
)

ACTIVE_SENDERS_WINDOW_DAYS = 7

_LLM_WORKLOADS = frozenset(
    {"email_agent", "memory_sanitizer", "abuse_judge", "embedding"}
)
_LLM_PROVIDERS = frozenset(
    {
        "anthropic",
        "bedrock",
        "cerebras",
        "cohere",
        "fireworks",
        "google-gla",
        "google-vertex",
        "groq",
        "huggingface",
        "mistral",
        "ollama",
        "openai",
        "openrouter",
        "other",
        "test",
        "xai",
    }
)
_LLM_OUTCOMES = frozenset({"success", "error"})
_LLM_COST_STATUSES = frozenset({"estimated", "unavailable"})
_LLM_TOKEN_TYPES = frozenset({"input", "output", "cache_read", "cache_write"})
_MAX_LLM_MODEL_LABELS = 8


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


@dataclass(frozen=True, slots=True)
class GrowthStateSnapshot:
    people_total: int
    activated_people_total: int
    active_senders_weekly: int


_GROWTH_STATE_QUERY = text(
    """
    SELECT
        (SELECT count(*) FROM people) AS people_total,
        (SELECT count(DISTINCT ref) FROM memories, unnest(refs) AS ref)
            AS activated_people_total,
        (SELECT count(DISTINCT ref) FROM memories, unnest(refs) AS ref
            WHERE created_at >= :cutoff) AS active_senders_weekly
    """
)


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


def collect_growth_state(
    *, session_factory=None, now: datetime | None = None
) -> GrowthStateSnapshot:
    """Read only aggregate people/memory counts, never per-person identity.

    ``active_senders_weekly`` is a proxy: people referenced by a memory created
    in the trailing window, not a literal distinct-authenticated-sender count -
    no table tracks per-person send timestamps without adding schema, which
    this observability addition must not do.
    """
    collected_at = now or _utcnow()
    cutoff = collected_at - timedelta(days=ACTIVE_SENDERS_WINDOW_DAYS)
    factory = session_factory or _metrics_session
    with factory() as session:
        row = next(
            iter(session.execute(_GROWTH_STATE_QUERY, {"cutoff": cutoff}).mappings())
        )
    return GrowthStateSnapshot(
        people_total=row["people_total"] or 0,
        activated_people_total=row["activated_people_total"] or 0,
        active_senders_weekly=row["active_senders_weekly"] or 0,
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


_network_density_lock = Lock()
_network_density_avg_degree = 0.0


def record_network_density(*, avg_degree: float) -> None:
    """Record the latest graph density sample without performing I/O or raising.

    Populated out-of-band from the existing hourly ``scan_for_opportunities``
    graph build (`thenetwork/worker/proactive.py`) - this never triggers a
    second graph computation.
    """
    global _network_density_avg_degree
    try:
        sampled = max(0.0, float(avg_degree))
        with _network_density_lock:
            _network_density_avg_degree = sampled
    except Exception:
        return


def network_density_avg_degree() -> float:
    with _network_density_lock:
        return _network_density_avg_degree


# Worker process and cgroup v2 self-metrics. These read local /proc and
# /sys/fs/cgroup files only - no Docker socket, no Engine API access, and
# nothing observed here ever leaves the current container. Every reader below
# is best effort: a missing file, an unreadable file, an unexpected format, or
# an unlimited ("max") cgroup limit is reported as "no observation" rather
# than raised, so a missing /proc entry or a non-cgroup-v2 host never gates
# the producer or worker.
_PROC_SELF_STATUS = Path("/proc/self/status")
_PROC_SELF_STAT = Path("/proc/self/stat")
_PROC_SELF_FD = Path("/proc/self/fd")
_CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_MEMORY_PEAK = Path("/sys/fs/cgroup/memory.peak")
_CGROUP_CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")

_clk_tck_lock = Lock()
_clk_tck: float | None = None


def _clock_ticks_per_second() -> float:
    global _clk_tck
    with _clk_tck_lock:
        if _clk_tck is None:
            try:
                _clk_tck = float(os.sysconf("SC_CLK_TCK"))
            except (ValueError, OSError, AttributeError):
                _clk_tck = 100.0
        return _clk_tck


def _read_status_field(field: str) -> int | None:
    try:
        for line in _PROC_SELF_STATUS.read_text().splitlines():
            if line.startswith(field):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1])
        return None
    except Exception:
        return None


def _read_process_resident_memory_bytes() -> int | None:
    kib = _read_status_field("VmRSS:")
    return kib * 1024 if kib is not None else None


def _read_process_threads() -> int | None:
    return _read_status_field("Threads:")


def _read_process_open_fds() -> int | None:
    try:
        return len(os.listdir(_PROC_SELF_FD))
    except Exception:
        return None


def _read_process_cpu_seconds() -> tuple[float, float] | None:
    """Return (user_seconds, system_seconds) from /proc/self/stat.

    Fields are positional and space-separated after the ``)`` that closes the
    (possibly space-containing) ``comm`` field; utime/stime are fields 14/15
    (1-indexed), i.e. indices 11/12 of the fields following ``comm``.
    """
    try:
        content = _PROC_SELF_STAT.read_text()
        closing = content.rindex(")")
        fields = content[closing + 2 :].split()
        if len(fields) < 13:
            return None
        ticks = _clock_ticks_per_second()
        return (int(fields[11]) / ticks, int(fields[12]) / ticks)
    except Exception:
        return None


def _read_cgroup_bytes(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
        if raw == "max":
            return None
        return int(raw)
    except Exception:
        return None


def _read_cgroup_cpu_stat() -> dict[str, int]:
    try:
        values: dict[str, int] = {}
        for line in _CGROUP_CPU_STAT.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    values[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        return values
    except Exception:
        return {}


def _process_resident_memory_callback(_options: object) -> list[Observation]:
    value = _read_process_resident_memory_bytes()
    return [Observation(value)] if value is not None else []


def _process_threads_callback(_options: object) -> list[Observation]:
    value = _read_process_threads()
    return [Observation(value)] if value is not None else []


def _process_open_fds_callback(_options: object) -> list[Observation]:
    value = _read_process_open_fds()
    return [Observation(value)] if value is not None else []


def _process_cpu_seconds_callback(_options: object) -> list[Observation]:
    result = _read_process_cpu_seconds()
    if result is None:
        return []
    user_seconds, system_seconds = result
    return [
        Observation(user_seconds, attributes={"state": "user"}),
        Observation(system_seconds, attributes={"state": "system"}),
    ]


def _cgroup_memory_current_callback(_options: object) -> list[Observation]:
    value = _read_cgroup_bytes(_CGROUP_MEMORY_CURRENT)
    return [Observation(value)] if value is not None else []


def _cgroup_memory_max_callback(_options: object) -> list[Observation]:
    value = _read_cgroup_bytes(_CGROUP_MEMORY_MAX)
    return [Observation(value)] if value is not None else []


def _cgroup_memory_peak_callback(_options: object) -> list[Observation]:
    value = _read_cgroup_bytes(_CGROUP_MEMORY_PEAK)
    return [Observation(value)] if value is not None else []


def _cgroup_cpu_periods_callback(_options: object) -> list[Observation]:
    stats = _read_cgroup_cpu_stat()
    return [Observation(stats["nr_periods"])] if "nr_periods" in stats else []


def _cgroup_cpu_throttled_periods_callback(_options: object) -> list[Observation]:
    stats = _read_cgroup_cpu_stat()
    return [Observation(stats["nr_throttled"])] if "nr_throttled" in stats else []


def _cgroup_cpu_throttled_seconds_callback(_options: object) -> list[Observation]:
    stats = _read_cgroup_cpu_stat()
    if "throttled_usec" not in stats:
        return []
    return [Observation(stats["throttled_usec"] / 1_000_000)]


_SnapshotT = TypeVar("_SnapshotT")


class _StateObserver(Generic[_SnapshotT]):
    """Share one contained database read across several gauge callbacks."""

    def __init__(
        self,
        collector: Callable[[], _SnapshotT] = collect_worker_state,
        *,
        cache_seconds: float = 1.0,
    ) -> None:
        self._collector = collector
        self._cache_seconds = cache_seconds
        self._lock = Lock()
        self._refresh_after = 0.0
        self._snapshot: _SnapshotT | None = None

    def snapshot(self) -> _SnapshotT | None:
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
_llm_requests_counter: object | None = None
_llm_tokens_counter: object | None = None
_llm_estimated_cost_counter: object | None = None
_llm_request_duration_histogram: object | None = None
_email_lifecycle_duration_histogram: object | None = None
_email_queue_duration_histogram: object | None = None
_agent_run_duration_histogram: object | None = None
_llm_model_labels_lock = Lock()
_registered_llm_model_labels = {"unknown"}


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


def register_llm_model_label(model: str) -> str:
    """Admit a deployment-configured model label into a small fixed registry."""
    candidate = model if model and len(model) <= 80 else "unknown"
    with _llm_model_labels_lock:
        if candidate in _registered_llm_model_labels:
            return candidate
        if len(_registered_llm_model_labels) >= _MAX_LLM_MODEL_LABELS:
            return "unknown"
        _registered_llm_model_labels.add(candidate)
        return candidate


def _registered_model_or_unknown(model: str) -> str:
    with _llm_model_labels_lock:
        return model if model in _registered_llm_model_labels else "unknown"


def _record_value(
    instrument: object | None,
    method: str,
    value: float | int,
    attributes: dict[str, str],
) -> None:
    if instrument is None:
        return
    try:
        getattr(instrument, method)(value, attributes=attributes)
    except Exception:
        return


def record_llm_request_metrics(
    *,
    workload: str,
    provider: str,
    model: str,
    outcome: str,
    cost_status: str,
    duration_seconds: float,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    estimated_cost_usd: float | None,
) -> None:
    """Record one request using only closed or deployment-bounded dimensions."""
    if (
        workload not in _LLM_WORKLOADS
        or provider not in _LLM_PROVIDERS
        or outcome not in _LLM_OUTCOMES
        or cost_status not in _LLM_COST_STATUSES
    ):
        return
    model = _registered_model_or_unknown(model)
    base_attributes = {
        "workload": workload,
        "provider": provider,
        "model": model,
        "outcome": outcome,
    }
    _record_value(
        _llm_requests_counter,
        "add",
        1,
        {**base_attributes, "cost_status": cost_status},
    )
    _record_value(
        _llm_request_duration_histogram,
        "record",
        max(0.0, duration_seconds),
        base_attributes,
    )
    token_values = {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read_tokens,
        "cache_write": cache_write_tokens,
    }
    for token_type, token_count in token_values.items():
        if token_type not in _LLM_TOKEN_TYPES or token_count is None:
            continue
        _record_value(
            _llm_tokens_counter,
            "add",
            max(0, token_count),
            {**base_attributes, "token_type": token_type},
        )
    if estimated_cost_usd is not None:
        _record_value(
            _llm_estimated_cost_counter,
            "add",
            max(0.0, estimated_cost_usd),
            {
                "workload": workload,
                "provider": provider,
                "model": model,
            },
        )


def record_email_lifecycle_metrics(
    *,
    outcome: str,
    total_duration_seconds: float | None,
    queue_duration_seconds: float | None,
    agent_duration_seconds: float | None,
) -> None:
    if outcome not in _LLM_OUTCOMES:
        return
    attributes = {"outcome": outcome}
    if total_duration_seconds is not None:
        _record_value(
            _email_lifecycle_duration_histogram,
            "record",
            max(0.0, total_duration_seconds),
            attributes,
        )
    if queue_duration_seconds is not None:
        _record_value(
            _email_queue_duration_histogram,
            "record",
            max(0.0, queue_duration_seconds),
            attributes,
        )
    if agent_duration_seconds is not None:
        _record_value(
            _agent_run_duration_histogram,
            "record",
            max(0.0, agent_duration_seconds),
            attributes,
        )


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
    growth_state_observer: _StateObserver | None = None,
) -> MeterProvider | None:
    """Start background OTLP export, returning ``None`` on setup failure."""
    global _agent_usage_limit_exceeded_counter
    global _control_actions_counter, _jobs_exhausted_counter
    global _agent_run_duration_histogram
    global _email_lifecycle_duration_histogram, _email_queue_duration_histogram
    global _llm_estimated_cost_counter, _llm_request_duration_histogram
    global _llm_requests_counter, _llm_tokens_counter
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
            growth_observer = growth_state_observer or _StateObserver(
                collect_growth_state
            )
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
                meter.create_observable_gauge(
                    PEOPLE_TOTAL_METRIC,
                    callbacks=[_state_callback(growth_observer, "people_total")],
                    unit="1",
                    description="Live count of registered people.",
                ),
                meter.create_observable_gauge(
                    ACTIVATED_PEOPLE_TOTAL_METRIC,
                    callbacks=[
                        _state_callback(growth_observer, "activated_people_total")
                    ],
                    unit="1",
                    description="Count of people referenced by at least one memory.",
                ),
                meter.create_observable_gauge(
                    ACTIVE_SENDERS_WEEKLY_METRIC,
                    callbacks=[
                        _state_callback(growth_observer, "active_senders_weekly")
                    ],
                    unit="1",
                    description=(
                        "People referenced by a memory created in the trailing "
                        f"{ACTIVE_SENDERS_WINDOW_DAYS}-day window, an "
                        "unlabeled proxy for distinct active senders."
                    ),
                ),
                meter.create_observable_gauge(
                    NETWORK_DENSITY_METRIC,
                    callbacks=[
                        lambda _options: [Observation(network_density_avg_degree())]
                    ],
                    unit="1",
                    description=(
                        "Average graph degree (2 * edges / nodes) sampled from "
                        "the hourly scan_for_opportunities graph build."
                    ),
                ),
                meter.create_observable_gauge(
                    WORKER_PROCESS_RESIDENT_MEMORY_METRIC,
                    callbacks=[_process_resident_memory_callback],
                    unit="By",
                    description=(
                        "Worker process resident memory, from /proc/self/status."
                    ),
                ),
                meter.create_observable_gauge(
                    WORKER_PROCESS_CPU_SECONDS_METRIC,
                    callbacks=[_process_cpu_seconds_callback],
                    unit="s",
                    description=(
                        "Worker process cumulative CPU time by state (user/system), "
                        "from /proc/self/stat."
                    ),
                ),
                meter.create_observable_gauge(
                    WORKER_PROCESS_OPEN_FDS_METRIC,
                    callbacks=[_process_open_fds_callback],
                    unit="1",
                    description=(
                        "Worker process open file descriptors, from /proc/self/fd."
                    ),
                ),
                meter.create_observable_gauge(
                    WORKER_PROCESS_THREADS_METRIC,
                    callbacks=[_process_threads_callback],
                    unit="1",
                    description="Worker process thread count, from /proc/self/status.",
                ),
                meter.create_observable_gauge(
                    WORKER_CGROUP_MEMORY_CURRENT_METRIC,
                    callbacks=[_cgroup_memory_current_callback],
                    unit="By",
                    description=(
                        "Current cgroup v2 memory usage, from "
                        "/sys/fs/cgroup/memory.current."
                    ),
                ),
                meter.create_observable_gauge(
                    WORKER_CGROUP_MEMORY_MAX_METRIC,
                    callbacks=[_cgroup_memory_max_callback],
                    unit="By",
                    description=(
                        "Configured cgroup v2 memory limit, from "
                        '/sys/fs/cgroup/memory.max. Absent when unlimited ("max").'
                    ),
                ),
                meter.create_observable_gauge(
                    WORKER_CGROUP_MEMORY_PEAK_METRIC,
                    callbacks=[_cgroup_memory_peak_callback],
                    unit="By",
                    description=(
                        "Peak cgroup v2 memory usage, from "
                        "/sys/fs/cgroup/memory.peak. Absent on older kernels."
                    ),
                ),
                meter.create_observable_counter(
                    WORKER_CGROUP_CPU_PERIODS_METRIC,
                    callbacks=[_cgroup_cpu_periods_callback],
                    unit="1",
                    description=(
                        "Elapsed cgroup v2 CPU scheduling periods, from "
                        "/sys/fs/cgroup/cpu.stat nr_periods."
                    ),
                ),
                meter.create_observable_counter(
                    WORKER_CGROUP_CPU_THROTTLED_PERIODS_METRIC,
                    callbacks=[_cgroup_cpu_throttled_periods_callback],
                    unit="1",
                    description=(
                        "Cgroup v2 CPU scheduling periods in which this container "
                        "was throttled, from /sys/fs/cgroup/cpu.stat nr_throttled."
                    ),
                ),
                meter.create_observable_counter(
                    WORKER_CGROUP_CPU_THROTTLED_SECONDS_METRIC,
                    callbacks=[_cgroup_cpu_throttled_seconds_callback],
                    unit="s",
                    description=(
                        "Cumulative cgroup v2 CPU throttled time, from "
                        "/sys/fs/cgroup/cpu.stat throttled_usec."
                    ),
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
            llm_requests_counter = meter.create_counter(
                LLM_REQUESTS_METRIC,
                unit="1",
                description="Logical model and embedding requests.",
            )
            llm_tokens_counter = meter.create_counter(
                LLM_TOKENS_METRIC,
                unit="{token}",
                description="Model and embedding tokens by token type.",
            )
            llm_estimated_cost_counter = meter.create_counter(
                LLM_ESTIMATED_COST_METRIC,
                unit="USD",
                description="Estimated model and embedding request cost in USD.",
            )
            llm_request_duration_histogram = meter.create_histogram(
                LLM_REQUEST_DURATION_METRIC,
                unit="s",
                description="Logical model and embedding request duration.",
            )
            email_lifecycle_duration_histogram = meter.create_histogram(
                EMAIL_LIFECYCLE_DURATION_METRIC,
                unit="s",
                description="Observed duration from inbox polling to task completion.",
            )
            email_queue_duration_histogram = meter.create_histogram(
                EMAIL_QUEUE_DURATION_METRIC,
                unit="s",
                description="Observed duration from inbox polling to task start.",
            )
            agent_run_duration_histogram = meter.create_histogram(
                AGENT_RUN_DURATION_METRIC,
                unit="s",
                description="Agent-run duration within a process-email attempt.",
            )
            instruments.extend(
                [
                    control_actions_counter,
                    agent_usage_limit_exceeded_counter,
                    jobs_exhausted_counter,
                    llm_requests_counter,
                    llm_tokens_counter,
                    llm_estimated_cost_counter,
                    llm_request_duration_histogram,
                    email_lifecycle_duration_histogram,
                    email_queue_duration_histogram,
                    agent_run_duration_histogram,
                ]
            )
            _instruments = instruments
            _control_actions_counter = control_actions_counter
            _agent_usage_limit_exceeded_counter = agent_usage_limit_exceeded_counter
            _jobs_exhausted_counter = jobs_exhausted_counter
            _llm_requests_counter = llm_requests_counter
            _llm_tokens_counter = llm_tokens_counter
            _llm_estimated_cost_counter = llm_estimated_cost_counter
            _llm_request_duration_histogram = llm_request_duration_histogram
            _email_lifecycle_duration_histogram = email_lifecycle_duration_histogram
            _email_queue_duration_histogram = email_queue_duration_histogram
            _agent_run_duration_histogram = agent_run_duration_histogram
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
