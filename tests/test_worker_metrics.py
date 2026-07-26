"""Tests for outbound-only worker liveness and state metrics."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from thenetwork.worker import metrics


class _Rows(list):
    def mappings(self):
        return self


class _Session:
    def __init__(self, *, rows=(), intake=None, execute_error=None):
        self.rows = _Rows(rows)
        self.intake = intake
        self.execute_error = execute_error
        self.parameters = None

    def execute(self, _query, parameters):
        self.parameters = parameters
        if self.execute_error is not None:
            raise self.execute_error
        return self.rows

    def get(self, model, key):
        assert model.__name__ == "PrimaryIntakeState"
        assert key == "primary"
        return self.intake


def _session_factory(session):
    @contextmanager
    def factory():
        yield session

    return factory


def test_pending_backlog_excludes_future_work_and_uses_due_time_for_age():
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    jobs = [
        metrics.PendingJobTiming(
            scheduled_at=None,
            enqueued_at=now - timedelta(seconds=90),
        ),
        metrics.PendingJobTiming(
            scheduled_at=now - timedelta(seconds=30),
            enqueued_at=now - timedelta(hours=1),
        ),
        metrics.PendingJobTiming(
            scheduled_at=now + timedelta(minutes=5),
            enqueued_at=now - timedelta(hours=2),
        ),
    ]

    assert metrics.summarize_pending_jobs(jobs, now=now) == (2, 90.0)
    assert metrics.summarize_pending_jobs([], now=now) == (0, 0.0)


@pytest.mark.parametrize(
    ("intake", "expected", "expected_reason"),
    [
        (None, 0, "none"),
        (SimpleNamespace(paused=False), 0, "none"),
        (SimpleNamespace(paused=True, pause_reason="admin"), 1, "admin"),
        (SimpleNamespace(paused=True, pause_reason="unbounded-value"), 1, "unknown"),
    ],
)
def test_collect_worker_state_reports_backlog_and_durable_intake(
    intake, expected, expected_reason
):
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    session = _Session(
        rows=[
            {
                "scheduled_at": None,
                "enqueued_at": now - timedelta(seconds=45),
            }
        ],
        intake=intake,
    )

    snapshot = metrics.collect_worker_state(
        session_factory=_session_factory(session), now=now
    )

    assert snapshot == metrics.WorkerStateSnapshot(
        queue_depth=1,
        oldest_pending_job_age_seconds=45.0,
        primary_intake_paused=expected,
        primary_intake_pause_reason=expected_reason,
    )
    assert session.parameters == {"now": now}


def test_collect_growth_state_reads_aggregate_counts_and_weekly_cutoff():
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    session = _Session(
        rows=[
            {
                "people_total": 42,
                "activated_people_total": 30,
                "active_senders_weekly": 12,
            }
        ]
    )

    snapshot = metrics.collect_growth_state(
        session_factory=_session_factory(session), now=now
    )

    assert snapshot == metrics.GrowthStateSnapshot(
        people_total=42, activated_people_total=30, active_senders_weekly=12
    )
    assert session.parameters == {
        "cutoff": now - timedelta(days=metrics.ACTIVE_SENDERS_WINDOW_DAYS)
    }


def test_collect_growth_state_treats_missing_counts_as_zero():
    session = _Session(
        rows=[
            {
                "people_total": None,
                "activated_people_total": None,
                "active_senders_weekly": None,
            }
        ]
    )

    snapshot = metrics.collect_growth_state(session_factory=_session_factory(session))

    assert snapshot == metrics.GrowthStateSnapshot(
        people_total=0, activated_people_total=0, active_senders_weekly=0
    )


def test_record_network_density_reports_last_sample_and_contains_bad_input():
    assert metrics.network_density_avg_degree() == 0.0

    metrics.record_network_density(avg_degree=3.5)
    assert metrics.network_density_avg_degree() == 3.5

    metrics.record_network_density(avg_degree=-1.0)
    assert metrics.network_density_avg_degree() == 0.0

    metrics.record_network_density(avg_degree="not-a-number")  # type: ignore[arg-type]
    assert metrics.network_density_avg_degree() == 0.0


def test_state_observer_contains_database_failure_without_values():
    observer = metrics._StateObserver(  # noqa: SLF001
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        cache_seconds=60,
    )

    with patch("thenetwork.worker.metrics.audit_warning_event") as warning:
        assert observer.snapshot() is None
        assert observer.snapshot() is None

    warning.assert_called_once_with(
        "worker.metrics_collection_failed", error_type="RuntimeError"
    )


class _FakeObservableCounter:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs


class _FakeMeter:
    def __init__(self):
        self.instruments = []

    def create_observable_gauge(self, name, **kwargs):
        instrument = {"name": name, **kwargs}
        self.instruments.append(instrument)
        return instrument

    def create_observable_counter(self, name, **kwargs):
        instrument = _FakeObservableCounter(name, **kwargs)
        self.instruments.append(instrument)
        return instrument

    def create_counter(self, name, **kwargs):
        instrument = _FakeCounter(name, **kwargs)
        self.instruments.append(instrument)
        return instrument

    def create_histogram(self, name, **kwargs):
        instrument = _FakeHistogram(name, **kwargs)
        self.instruments.append(instrument)
        return instrument


class _FakeCounter:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.calls = []

    def add(self, value, *, attributes=None):
        self.calls.append((value, attributes))


class _FakeHistogram:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.calls = []

    def record(self, value, *, attributes=None):
        self.calls.append((value, attributes))


class _FakeProvider:
    def __init__(self, *, metric_readers, resource=None):
        self.metric_readers = metric_readers
        self.resource = resource
        self.meter = _FakeMeter()

    def get_meter(self, name):
        assert name == "thenetwork.worker"
        return self.meter


@pytest.fixture(autouse=True)
def _reset_metric_globals(monkeypatch):
    monkeypatch.setattr(metrics, "_meter_provider", None)
    monkeypatch.setattr(metrics, "_instruments", [])
    monkeypatch.setattr(metrics, "_control_actions_counter", None)
    monkeypatch.setattr(metrics, "_agent_usage_limit_exceeded_counter", None)
    monkeypatch.setattr(metrics, "_jobs_exhausted_counter", None)
    monkeypatch.setattr(metrics, "_llm_requests_counter", None)
    monkeypatch.setattr(metrics, "_llm_tokens_counter", None)
    monkeypatch.setattr(metrics, "_llm_estimated_cost_counter", None)
    monkeypatch.setattr(metrics, "_llm_request_duration_histogram", None)
    monkeypatch.setattr(metrics, "_email_lifecycle_duration_histogram", None)
    monkeypatch.setattr(metrics, "_email_queue_duration_histogram", None)
    monkeypatch.setattr(metrics, "_agent_run_duration_histogram", None)
    monkeypatch.setattr(metrics, "_registered_llm_model_labels", {"unknown"})
    monkeypatch.setattr(metrics, "_producer_last_success_timestamp_seconds", 0.0)
    monkeypatch.setattr(metrics, "_network_density_avg_degree", 0.0)
    monkeypatch.setattr(metrics, "_clk_tck", None)


def _write_process_and_cgroup_fixtures(monkeypatch, tmp_path):
    """Point every /proc and /sys/fs/cgroup path constant at controlled files."""
    status_path = tmp_path / "status"
    status_path.write_text("VmRSS:      2048 kB\nThreads:         4\n")
    stat_path = tmp_path / "stat"
    # Fields after the ")" that closes comm; utime/stime are indices 11/12.
    stat_path.write_text(
        "1234 (worker) S 1 1 1 0 -1 0 0 0 0 0 500 250 0 0 20 0 1 0 100\n"
    )
    fd_dir = tmp_path / "fd"
    fd_dir.mkdir()
    for fd in range(3):
        (fd_dir / str(fd)).write_text("")
    memory_current = tmp_path / "memory.current"
    memory_current.write_text("209715200\n")
    memory_max = tmp_path / "memory.max"
    memory_max.write_text("536870912\n")
    memory_peak = tmp_path / "memory.peak"
    memory_peak.write_text("314572800\n")
    cpu_stat = tmp_path / "cpu.stat"
    cpu_stat.write_text(
        "usage_usec 123456\nnr_periods 40\nnr_throttled 3\nthrottled_usec 250000\n"
    )
    monkeypatch.setattr(metrics, "_PROC_SELF_STATUS", status_path)
    monkeypatch.setattr(metrics, "_PROC_SELF_STAT", stat_path)
    monkeypatch.setattr(metrics, "_PROC_SELF_FD", fd_dir)
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_CURRENT", memory_current)
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_MAX", memory_max)
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_PEAK", memory_peak)
    monkeypatch.setattr(metrics, "_CGROUP_CPU_STAT", cpu_stat)
    monkeypatch.setattr(metrics, "_clock_ticks_per_second", lambda: 100.0)


def test_metric_names_units_values_and_bounded_labels(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        worker_metrics_otlp_endpoint="http://collector:4318/v1/metrics",
        worker_metrics_export_interval_seconds=30,
        worker_metrics_export_timeout_seconds=5,
    )
    monkeypatch.setattr(metrics, "get_settings", lambda: settings)
    _write_process_and_cgroup_fixtures(monkeypatch, tmp_path)
    exporter_calls = []
    reader_calls = []

    def exporter_factory(**kwargs):
        exporter_calls.append(kwargs)
        return object()

    def reader_factory(exporter, **kwargs):
        reader_calls.append((exporter, kwargs))
        return object()

    observer = metrics._StateObserver(  # noqa: SLF001
        lambda: metrics.WorkerStateSnapshot(7, 120.5, 1, "coordinated_abuse"),
        cache_seconds=60,
    )
    growth_observer = metrics._StateObserver(  # noqa: SLF001
        lambda: metrics.GrowthStateSnapshot(42, 30, 12),
        cache_seconds=60,
    )
    metrics.record_producer_poll_success(timestamp=1_784_732_400)
    metrics.record_network_density(avg_degree=2.5)

    provider = metrics.configure_worker_metrics(
        exporter_factory=exporter_factory,
        reader_factory=reader_factory,
        provider_factory=_FakeProvider,
        state_observer=observer,
        growth_state_observer=growth_observer,
    )

    assert exporter_calls == [
        {"endpoint": "http://collector:4318/v1/metrics", "timeout": 5}
    ]
    assert reader_calls[0][1] == {
        "export_interval_millis": 30_000,
        "export_timeout_millis": 5_000,
    }
    instruments = provider.meter.instruments
    gauges = [item for item in instruments if isinstance(item, dict)]
    assert [(item["name"], item["unit"]) for item in gauges] == [
        (metrics.PRODUCER_LAST_SUCCESS_METRIC, "s"),
        (metrics.JOB_QUEUE_DEPTH_METRIC, "{jobs}"),
        (metrics.OLDEST_PENDING_JOB_AGE_METRIC, "s"),
        (metrics.PRIMARY_INTAKE_PAUSED_METRIC, "{paused}"),
        (metrics.PEOPLE_TOTAL_METRIC, "{people}"),
        (metrics.ACTIVATED_PEOPLE_TOTAL_METRIC, "{people}"),
        (metrics.ACTIVE_SENDERS_WEEKLY_METRIC, "{people}"),
        (metrics.NETWORK_DENSITY_METRIC, "{degree}"),
        (metrics.WORKER_PROCESS_RESIDENT_MEMORY_METRIC, "By"),
        (metrics.WORKER_PROCESS_CPU_SECONDS_METRIC, "s"),
        (metrics.WORKER_PROCESS_OPEN_FDS_METRIC, "{fds}"),
        (metrics.WORKER_PROCESS_THREADS_METRIC, "{threads}"),
        (metrics.WORKER_CGROUP_MEMORY_CURRENT_METRIC, "By"),
        (metrics.WORKER_CGROUP_MEMORY_MAX_METRIC, "By"),
        (metrics.WORKER_CGROUP_MEMORY_PEAK_METRIC, "By"),
    ]
    observable_counters = [
        item for item in instruments if isinstance(item, _FakeObservableCounter)
    ]
    assert [(item.name, item.kwargs["unit"]) for item in observable_counters] == [
        (metrics.WORKER_CGROUP_CPU_PERIODS_METRIC, "1"),
        (metrics.WORKER_CGROUP_CPU_THROTTLED_PERIODS_METRIC, "1"),
        (metrics.WORKER_CGROUP_CPU_THROTTLED_SECONDS_METRIC, "s"),
    ]
    assert [
        item.kwargs["callbacks"][0](None)[0].value for item in observable_counters
    ] == [40, 3, 0.25]
    counters = [item for item in instruments if isinstance(item, _FakeCounter)]
    assert [item.name for item in counters] == [
        metrics.CONTROL_ACTIONS_METRIC,
        metrics.AGENT_USAGE_LIMIT_EXCEEDED_METRIC,
        metrics.JOBS_EXHAUSTED_METRIC,
        metrics.LLM_REQUESTS_METRIC,
        metrics.LLM_TOKENS_METRIC,
        metrics.LLM_ESTIMATED_COST_METRIC,
    ]
    assert [item.kwargs["unit"] for item in counters] == [
        "1",
        "1",
        "1",
        "1",
        "{token}",
        "USD",
    ]
    histograms = [item for item in instruments if isinstance(item, _FakeHistogram)]
    assert [item.name for item in histograms] == [
        metrics.LLM_REQUEST_DURATION_METRIC,
        metrics.EMAIL_LIFECYCLE_DURATION_METRIC,
        metrics.EMAIL_QUEUE_DURATION_METRIC,
        metrics.AGENT_RUN_DURATION_METRIC,
    ]
    assert all(item.kwargs["unit"] == "s" for item in histograms)
    observations = [item["callbacks"][0](None)[0] for item in gauges]
    assert [item.value for item in observations] == [
        1_784_732_400,
        7,
        120.5,
        1,
        42,
        30,
        12,
        2.5,
        2_097_152,
        5.0,
        3,
        4,
        209_715_200,
        536_870_912,
        314_572_800,
    ]
    assert all(not item.attributes for item in observations[:3])
    assert observations[3].attributes == {"reason": "coordinated_abuse"}
    cpu_seconds_index = 9
    assert all(
        not item.attributes
        for i, item in enumerate(observations[4:])
        if 4 + i != cpu_seconds_index
    )
    assert observations[cpu_seconds_index].attributes == {"state": "user"}
    cpu_seconds_observations = [
        item
        for item in gauges
        if item["name"] == metrics.WORKER_PROCESS_CPU_SECONDS_METRIC
    ][0]["callbacks"][0](None)
    assert [obs.value for obs in cpu_seconds_observations] == [5.0, 2.5]
    assert [obs.attributes for obs in cpu_seconds_observations] == [
        {"state": "user"},
        {"state": "system"},
    ]


def test_process_and_cgroup_readers_are_best_effort_on_missing_or_malformed_files(
    monkeypatch, tmp_path
):
    missing_dir = tmp_path / "missing"
    monkeypatch.setattr(metrics, "_PROC_SELF_STATUS", missing_dir / "status")
    monkeypatch.setattr(metrics, "_PROC_SELF_STAT", missing_dir / "stat")
    monkeypatch.setattr(metrics, "_PROC_SELF_FD", missing_dir / "fd")
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_CURRENT", missing_dir / "current")
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_MAX", missing_dir / "max")
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_PEAK", missing_dir / "peak")
    monkeypatch.setattr(metrics, "_CGROUP_CPU_STAT", missing_dir / "cpu.stat")

    assert metrics._process_resident_memory_callback(None) == []  # noqa: SLF001
    assert metrics._process_threads_callback(None) == []  # noqa: SLF001
    assert metrics._process_open_fds_callback(None) == []  # noqa: SLF001
    assert metrics._process_cpu_seconds_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_memory_current_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_memory_max_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_memory_peak_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_cpu_periods_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_cpu_throttled_periods_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_cpu_throttled_seconds_callback(None) == []  # noqa: SLF001

    # An unlimited cgroup memory.max reads "max", not a number - never raise,
    # never fabricate a byte value.
    memory_max = tmp_path / "memory.max"
    memory_max.write_text("max\n")
    monkeypatch.setattr(metrics, "_CGROUP_MEMORY_MAX", memory_max)
    assert metrics._cgroup_memory_max_callback(None) == []  # noqa: SLF001

    # A malformed /proc/self/stat (too few fields after comm) is also absorbed.
    stat_path = tmp_path / "stat"
    stat_path.write_text("1234 (worker) S 1\n")
    monkeypatch.setattr(metrics, "_PROC_SELF_STAT", stat_path)
    assert metrics._process_cpu_seconds_callback(None) == []  # noqa: SLF001

    # A cpu.stat missing the throttling keys yields no observation for them,
    # without raising or fabricating a value.
    cpu_stat = tmp_path / "cpu.stat"
    cpu_stat.write_text("usage_usec 100\n")
    monkeypatch.setattr(metrics, "_CGROUP_CPU_STAT", cpu_stat)
    assert metrics._cgroup_cpu_periods_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_cpu_throttled_periods_callback(None) == []  # noqa: SLF001
    assert metrics._cgroup_cpu_throttled_seconds_callback(None) == []  # noqa: SLF001


def test_operational_counters_use_only_closed_dimensions_and_reset_cleanly(monkeypatch):
    control = _FakeCounter(metrics.CONTROL_ACTIONS_METRIC)
    usage = _FakeCounter(metrics.AGENT_USAGE_LIMIT_EXCEEDED_METRIC)
    exhausted = _FakeCounter(metrics.JOBS_EXHAUSTED_METRIC)
    monkeypatch.setattr(metrics, "_control_actions_counter", control)
    monkeypatch.setattr(metrics, "_agent_usage_limit_exceeded_counter", usage)
    monkeypatch.setattr(metrics, "_jobs_exhausted_counter", exhausted)

    metrics.record_control_action(
        action=metrics.ControlAction.PAUSE,
        actor=metrics.ControlActor.SYSTEM,
        reason=metrics.ControlReason.NEW_SENDER_BURST,
    )
    metrics.record_agent_usage_limit_exceeded()
    metrics.record_job_exhausted()
    metrics.record_control_action(
        action="attacker-selected",  # type: ignore[arg-type]
        actor=metrics.ControlActor.SYSTEM,
        reason=metrics.ControlReason.NEW_SENDER_BURST,
    )

    assert control.calls == [
        (
            1,
            {
                "action": "pause",
                "actor": "system",
                "reason": "new_sender_burst",
            },
        )
    ]
    assert usage.calls == [(1, None)]
    assert exhausted.calls == [(1, None)]

    monkeypatch.setattr(metrics, "_control_actions_counter", None)
    monkeypatch.setattr(metrics, "_agent_usage_limit_exceeded_counter", None)
    monkeypatch.setattr(metrics, "_jobs_exhausted_counter", None)
    metrics.record_agent_usage_limit_exceeded()
    metrics.record_job_exhausted()
    assert usage.calls == [(1, None)]
    assert exhausted.calls == [(1, None)]


def test_counter_failure_is_contained():
    class _FailingCounter:
        def add(self, _value, *, attributes=None):
            raise RuntimeError("exporter unavailable")

    metrics._control_actions_counter = _FailingCounter()  # noqa: SLF001
    metrics._agent_usage_limit_exceeded_counter = _FailingCounter()  # noqa: SLF001
    metrics._jobs_exhausted_counter = _FailingCounter()  # noqa: SLF001

    metrics.record_control_action(
        action=metrics.ControlAction.BAN,
        actor=metrics.ControlActor.SYSTEM,
        reason=metrics.ControlReason.AUTOMATIC_POLICY,
    )
    metrics.record_agent_usage_limit_exceeded()
    metrics.record_job_exhausted()


def test_llm_metrics_use_registered_bounded_dimensions(monkeypatch):
    requests = _FakeCounter(metrics.LLM_REQUESTS_METRIC)
    tokens = _FakeCounter(metrics.LLM_TOKENS_METRIC)
    cost = _FakeCounter(metrics.LLM_ESTIMATED_COST_METRIC)
    duration = _FakeHistogram(metrics.LLM_REQUEST_DURATION_METRIC)
    monkeypatch.setattr(metrics, "_llm_requests_counter", requests)
    monkeypatch.setattr(metrics, "_llm_tokens_counter", tokens)
    monkeypatch.setattr(metrics, "_llm_estimated_cost_counter", cost)
    monkeypatch.setattr(metrics, "_llm_request_duration_histogram", duration)

    assert metrics.register_llm_model_label("gpt-4.1-mini") == "gpt-4.1-mini"
    metrics.record_llm_request_metrics(
        workload="email_agent",
        provider="openai",
        model="gpt-4.1-mini",
        outcome="success",
        cost_status="estimated",
        duration_seconds=1.25,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=10,
        cache_write_tokens=0,
        estimated_cost_usd=0.004,
    )
    metrics.record_llm_request_metrics(
        workload="attacker-selected",
        provider="openai",
        model="unregistered-model",
        outcome="success",
        cost_status="estimated",
        duration_seconds=1,
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        estimated_cost_usd=1,
    )

    base = {
        "workload": "email_agent",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "outcome": "success",
    }
    assert requests.calls == [(1, {**base, "cost_status": "estimated"})]
    assert duration.calls == [(1.25, base)]
    assert tokens.calls == [
        (100, {**base, "token_type": "input"}),
        (20, {**base, "token_type": "output"}),
        (10, {**base, "token_type": "cache_read"}),
        (0, {**base, "token_type": "cache_write"}),
    ]
    assert cost.calls == [
        (
            0.004,
            {
                "workload": "email_agent",
                "provider": "openai",
                "model": "gpt-4.1-mini",
            },
        )
    ]


def test_email_lifecycle_metrics_skip_missing_intake_duration(monkeypatch):
    lifecycle = _FakeHistogram(metrics.EMAIL_LIFECYCLE_DURATION_METRIC)
    queue = _FakeHistogram(metrics.EMAIL_QUEUE_DURATION_METRIC)
    agent = _FakeHistogram(metrics.AGENT_RUN_DURATION_METRIC)
    monkeypatch.setattr(metrics, "_email_lifecycle_duration_histogram", lifecycle)
    monkeypatch.setattr(metrics, "_email_queue_duration_histogram", queue)
    monkeypatch.setattr(metrics, "_agent_run_duration_histogram", agent)

    metrics.record_email_lifecycle_metrics(
        outcome="success",
        total_duration_seconds=None,
        queue_duration_seconds=None,
        agent_duration_seconds=2.5,
    )

    assert lifecycle.calls == []
    assert queue.calls == []
    assert agent.calls == [(2.5, {"outcome": "success"})]

    metrics.record_email_lifecycle_metrics(
        outcome="success",
        total_duration_seconds=None,
        queue_duration_seconds=None,
        agent_duration_seconds=None,
    )

    assert agent.calls == [(2.5, {"outcome": "success"})]


def test_metric_exporter_setup_failure_is_contained(monkeypatch):
    monkeypatch.setattr(
        metrics,
        "get_settings",
        lambda: SimpleNamespace(
            worker_metrics_otlp_endpoint="http://collector:4318/v1/metrics",
            worker_metrics_export_timeout_seconds=5,
        ),
    )

    with patch("thenetwork.worker.metrics.audit_warning_event") as warning:
        result = metrics.configure_worker_metrics(
            exporter_factory=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("collector unavailable")
            )
        )

    assert result is None
    warning.assert_called_once_with(
        "worker.metrics_setup_failed", error_type="RuntimeError"
    )
    metrics.record_producer_poll_success(timestamp=123.0)
    assert metrics.producer_last_success_timestamp_seconds() == 123.0


def test_successful_empty_poll_advances_timestamp_and_failed_poll_does_not():
    with (
        patch(
            "thenetwork.worker.producer.relay_mailbox_configured",
            return_value=False,
        ),
        patch(
            "thenetwork.worker.producer._poll_mailbox_and_enqueue",
            return_value=0,
        ),
        patch(
            "thenetwork.worker.producer.is_primary_intake_paused",
            return_value=False,
        ),
        patch(
            "thenetwork.worker.producer.record_producer_poll_success"
        ) as record_success,
    ):
        from thenetwork.worker.producer import _poll_and_enqueue

        assert _poll_and_enqueue() == 0
        record_success.assert_called_once_with()

    with (
        patch(
            "thenetwork.worker.producer.relay_mailbox_configured",
            return_value=False,
        ),
        patch(
            "thenetwork.worker.producer._poll_mailbox_and_enqueue",
            side_effect=RuntimeError("poll failed"),
        ),
        patch(
            "thenetwork.worker.producer.is_primary_intake_paused",
            return_value=False,
        ),
        patch(
            "thenetwork.worker.producer.record_producer_poll_success"
        ) as record_success,
    ):
        with pytest.raises(RuntimeError, match="poll failed"):
            _poll_and_enqueue()
        record_success.assert_not_called()
