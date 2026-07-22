"""Tests for the OpenTelemetry Collector transform processor behavior.

Validates that the collector's transform pipeline (as configured in
otel-collector-config.yaml) correctly parses worker JSON log bodies into
structured LogRecord attributes and clears the body after parsing.
"""

from collections import Counter
import json
import pathlib
import re

import yaml


# ---------------------------------------------------------------------------
# Load the actual collector config so tests break when the config changes
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COLLECTOR_CONFIG = yaml.safe_load(
    (_REPO_ROOT / "otel-collector-config.yaml").read_text()
)
_COMPOSE_CONFIG = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text())
_PROMETHEUS_CONFIG = yaml.safe_load((_REPO_ROOT / "prometheus.yml").read_text())
_WORKER_METRIC_SOURCE_CONFIG = yaml.safe_load(
    (_REPO_ROOT / "tests/fixtures/otel-worker-metrics-source.yaml").read_text()
)


def _condition_matches(condition: str, attributes: dict) -> bool:
    """Evaluate the deliberately small OTTL subset used by count/audit."""
    for clause in condition.split(" and "):
        equality = re.fullmatch(r'attributes\["([^"]+)"\] == "([^"]+)"', clause)
        if equality:
            if attributes.get(equality.group(1)) != equality.group(2):
                return False
            continue

        is_string = re.fullmatch(r'IsString\(attributes\["([^"]+)"\]\)', clause)
        if is_string:
            if not isinstance(attributes.get(is_string.group(1)), str):
                return False
            continue

        is_match = re.fullmatch(
            r'IsMatch\(attributes\["([^"]+)"\], "([^"]+)"\)', clause
        )
        if is_match:
            value = attributes.get(is_match.group(1))
            if (
                not isinstance(value, str)
                or re.fullmatch(is_match.group(2), value) is None
            ):
                return False
            continue

        raise AssertionError(f"unsupported count/audit condition clause: {clause}")
    return True


def _derived_metric_counts(records: list[dict]) -> Counter:
    """Simulate count/audit routing using the checked-in configuration."""
    counts = Counter()
    metrics = _COLLECTOR_CONFIG["connectors"]["count/audit"]["logs"]
    for attributes in records:
        for metric_name, metric in metrics.items():
            if not any(
                _condition_matches(condition, attributes)
                for condition in metric["conditions"]
            ):
                continue
            labels = tuple(
                (attribute["key"], attributes[attribute["key"]])
                for attribute in metric.get("attributes", [])
            )
            counts[(metric_name, labels)] += 1
    return counts


def _process_json_log_record(raw_record: dict) -> dict:
    """Simulate the OpenTelemetry Collector transform processor.

    Mirrors the two OTTL statements in otel-collector-config.yaml:
      1. merge_maps(attributes, ParseJSON(body), "upsert") where IsString(body)
      2. set(body, "") where IsString(body)

    The fluentd logging driver delivers each container log line as a record
    with the JSON payload in the ``log`` field plus Docker-added metadata
    (``container_name``, ``source``).  The Collector's fluentforward receiver
    maps the ``log`` field to the LogRecord body.

    Returns a dict with ``body`` (cleared after successful parse) and all
    attributes (Docker metadata merged with parsed JSON fields).
    """
    body = raw_record.get("log", "")
    attributes = {k: v for k, v in raw_record.items() if k != "log"}

    if isinstance(body, str):
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict):
                # OTTL: merge_maps(attributes, ParseJSON(body), "upsert")
                attributes.update(parsed)
                # OTTL: set(body, "") where IsString(body)
                body = ""
        except json.JSONDecodeError:
            # Non-JSON lines (e.g. raw stderr) keep their body intact.
            pass

    return {"body": body, **attributes}


# ---------------------------------------------------------------------------
# Config-level checks
# ---------------------------------------------------------------------------


def test_collector_config_has_transform_processor():
    """The collector config includes the transform processor with the expected
    OTTL statements for JSON parsing and body clearing."""
    transform = _COLLECTOR_CONFIG.get("processors", {}).get("transform", {})
    stmts = transform.get("log_statements", [{}])[0].get("statements", [])
    assert any("ParseJSON" in s for s in stmts), (
        "transform processor must parse JSON body"
    )
    assert any('set(body, "")' in s for s in stmts), (
        "transform processor must clear body after parse"
    )


def test_collector_config_has_health_check_extension():
    """The collector config enables the health_check extension for Docker
    healthcheck support."""
    extensions = _COLLECTOR_CONFIG.get("extensions", {})
    assert "health_check" in extensions, (
        "collector config must include health_check extension"
    )
    service_extensions = _COLLECTOR_CONFIG.get("service", {}).get("extensions", [])
    assert "health_check" in service_extensions, (
        "health_check must be listed in service.extensions"
    )


def test_collector_has_no_required_external_exporter():
    """The local monitoring stack starts without an external OTLP backend."""
    collector = _COMPOSE_CONFIG["services"]["otel-collector"]
    assert "environment" not in collector
    assert set(_COLLECTOR_CONFIG["exporters"]) == {"prometheus/audit"}
    assert _COLLECTOR_CONFIG["service"]["pipelines"]["logs"]["exporters"] == [
        "count/audit"
    ]


def test_collector_derives_bounded_counter_catalog_from_redacted_audit_logs():
    count_config = _COLLECTOR_CONFIG["connectors"]["count/audit"]["logs"]
    assert set(count_config) == {
        "thenetwork.worker.audit.events",
        "thenetwork.accounts.created",
        "thenetwork.messages.processed",
        "thenetwork.messages.rejected",
        "thenetwork.agent.runs",
        "thenetwork.agent.tool.calls",
        "thenetwork.introduction.transitions",
        "thenetwork.outbound.emails",
        "thenetwork.relay.messages.forwarded",
    }
    activity_metric = count_config["thenetwork.worker.audit.events"]
    assert activity_metric["conditions"] == [
        'attributes["logger"] == "thenetwork.audit"'
    ]
    assert "attributes" not in activity_metric, (
        "the audit counter must not project log attributes into metric labels"
    )

    expected_labels = {
        "thenetwork.accounts.created": [],
        "thenetwork.messages.processed": ["outcome"],
        "thenetwork.messages.rejected": ["reason"],
        "thenetwork.agent.runs": ["outcome"],
        "thenetwork.agent.tool.calls": ["tool_name", "tool_outcome"],
        "thenetwork.introduction.transitions": ["action", "consent_state"],
        "thenetwork.outbound.emails": ["outcome", "template_id"],
        "thenetwork.relay.messages.forwarded": [],
    }
    for metric_name, label_names in expected_labels.items():
        metric = count_config[metric_name]
        assert [item["key"] for item in metric.get("attributes", [])] == label_names
        for label_name in label_names:
            condition = " ".join(metric["conditions"])
            assert f'IsString(attributes["{label_name}"])' in condition
            assert f'IsMatch(attributes["{label_name}"]' in condition

    pipelines = _COLLECTOR_CONFIG["service"]["pipelines"]
    assert pipelines["logs"]["exporters"] == ["count/audit"]
    assert pipelines["metrics/audit"] == {
        "receivers": ["count/audit", "otlp/worker_metrics"],
        "exporters": ["prometheus/audit"],
    }
    assert _COLLECTOR_CONFIG["receivers"]["otlp/worker_metrics"] == {
        "protocols": {"http": {"endpoint": "0.0.0.0:4318"}}
    }
    assert _COLLECTOR_CONFIG["exporters"]["prometheus/audit"]["endpoint"] == (
        "0.0.0.0:8889"
    )


def test_representative_audit_records_increment_each_product_counter_once():
    records = [
        {
            "logger": "thenetwork.audit",
            "event": "database.action",
            "action": "insert",
            "record_type": "person",
            "outcome": "success",
        },
        {
            "logger": "thenetwork.audit",
            "event": "worker.process_email.completed",
            "outcome": "success",
        },
        {
            "logger": "thenetwork.audit",
            "event": "worker.message_rejected",
            "reason": "rate_limit",
        },
        {
            "logger": "thenetwork.audit",
            "event": "agent.run.completed",
            "outcome": "success",
        },
        {
            "logger": "thenetwork.audit",
            "event": "agent.tool.completed",
            "tool_name": "remember",
            "tool_outcome": "created",
        },
        {
            "logger": "thenetwork.audit",
            "event": "introduction.consent_transition",
            "outcome": "success",
            "action": "consent",
            "consent_state": "introduced",
        },
        {
            "logger": "thenetwork.audit",
            "event": "email.smtp_send.completed",
            "outcome": "success",
            "template_id": "conversational",
        },
        {
            "logger": "thenetwork.audit",
            "event": "worker.relay_forwarded",
            "outcome": "success",
        },
    ]

    counts = _derived_metric_counts(records)

    product_counts = {
        metric_name: value
        for (metric_name, _labels), value in counts.items()
        if metric_name != "thenetwork.worker.audit.events"
    }
    assert product_counts == {
        "thenetwork.accounts.created": 1,
        "thenetwork.messages.processed": 1,
        "thenetwork.messages.rejected": 1,
        "thenetwork.agent.runs": 1,
        "thenetwork.agent.tool.calls": 1,
        "thenetwork.introduction.transitions": 1,
        "thenetwork.outbound.emails": 1,
        "thenetwork.relay.messages.forwarded": 1,
    }


def test_retry_and_replay_outcomes_remain_distinguishable():
    counts = _derived_metric_counts(
        [
            {
                "logger": "thenetwork.audit",
                "event": "worker.process_email.completed",
                "outcome": "error",
            },
            {
                "logger": "thenetwork.audit",
                "event": "worker.process_email.completed",
                "outcome": "success",
            },
            {
                "logger": "thenetwork.audit",
                "event": "agent.tool.completed",
                "tool_name": "remember",
                "tool_outcome": "replayed",
            },
        ]
    )

    assert counts[("thenetwork.messages.processed", (("outcome", "error"),))] == 1
    assert counts[("thenetwork.messages.processed", (("outcome", "success"),))] == 1
    assert (
        counts[
            (
                "thenetwork.agent.tool.calls",
                (("tool_name", "remember"), ("tool_outcome", "replayed")),
            )
        ]
        == 1
    )


def test_malformed_records_and_registration_failures_create_no_product_series():
    records = [
        {
            "logger": "thenetwork.audit",
            "event": "database.action",
            "action": "insert",
            "record_type": "person",
            "outcome": outcome,
            "trace_id": "unsafe-cardinality-value",
        }
        for outcome in (
            "exists",
            "rate_limited",
            "rejected_already_registered",
            "rejected_unauthenticated",
        )
    ]
    records.extend(
        [
            {
                "logger": "thenetwork.audit",
                "event": "agent.tool.completed",
                "tool_name": "attacker_selected_tool",
                "tool_outcome": "sent",
                "sender_id_hash": "attacker-selected-identifier",
            },
            {
                "logger": "thenetwork.audit",
                "event": "email.smtp_send.completed",
                "outcome": "success",
                "template_id": "attacker_selected_template",
            },
            {"logger": "thenetwork.audit", "event": "unknown.event"},
            {"logger": "foreign.logger", "event": "agent.run.completed"},
        ]
    )

    counts = _derived_metric_counts(records)

    assert {
        metric_name
        for metric_name, _labels in counts
        if metric_name != "thenetwork.worker.audit.events"
    } == set()


def test_prometheus_scrapes_collector_health_and_audit_activity():
    assert _COLLECTOR_CONFIG["service"]["telemetry"]["metrics"]["address"] == (
        "0.0.0.0:8888"
    )
    jobs = {
        job["job_name"]: job["static_configs"][0]["targets"]
        for job in _PROMETHEUS_CONFIG["scrape_configs"]
    }
    assert jobs == {
        "otel-collector-internal": ["otel-collector:8888"],
        "thenetwork-audit-activity": ["otel-collector:8889"],
    }


def test_prometheus_service_is_pinned_private_and_persistent():
    compose = _COMPOSE_CONFIG
    prometheus = compose["services"]["prometheus"]

    assert prometheus["image"] == "prom/prometheus:v3.5.5"
    assert prometheus["ports"] == ["127.0.0.1:9090:9090"]
    assert "prometheus-data:/prometheus" in prometheus["volumes"]
    assert "prometheus-data" in compose["volumes"]
    assert "--storage.tsdb.retention.time=30d" in prometheus["command"]
    assert prometheus["depends_on"]["otel-collector"]["condition"] == (
        "service_started"
    )
    assert "healthcheck" not in compose["services"]["otel-collector"], (
        "the collector image has no wget/curl; scrape targets prove readiness"
    )
    assert "ports" not in compose["services"]["worker"], (
        "worker metrics must remain collector-derived with no inbound port"
    )


def test_worker_state_metrics_use_internal_outbound_otlp_path():
    worker = _COMPOSE_CONFIG["services"]["worker"]
    expected_environment = {
        "WORKER_METRICS_OTLP_ENDPOINT": (
            "${WORKER_METRICS_OTLP_ENDPOINT:-http://otel-collector:4318/v1/metrics}"
        ),
        "WORKER_METRICS_EXPORT_INTERVAL_SECONDS": (
            "${WORKER_METRICS_EXPORT_INTERVAL_SECONDS:-30}"
        ),
        "WORKER_METRICS_EXPORT_TIMEOUT_SECONDS": (
            "${WORKER_METRICS_EXPORT_TIMEOUT_SECONDS:-5}"
        ),
        "WORKER_METRICS_COLLECTION_TIMEOUT_SECONDS": (
            "${WORKER_METRICS_COLLECTION_TIMEOUT_SECONDS:-2}"
        ),
    }
    for name, value in expected_environment.items():
        assert worker["environment"][name] == value
    assert "ports" not in worker

    pipeline = _WORKER_METRIC_SOURCE_CONFIG["service"]["pipelines"]["metrics"]
    assert pipeline == {"receivers": ["prometheus"], "exporters": ["otlphttp"]}
    assert _WORKER_METRIC_SOURCE_CONFIG["exporters"]["otlphttp"]["endpoint"] == (
        "http://otel-collector:4318"
    )

    fixture = (_REPO_ROOT / "tests/fixtures/worker-metrics/metrics").read_text()
    expected_metrics = {
        "thenetwork_producer_last_success_timestamp_seconds",
        "thenetwork_job_queue_depth",
        "thenetwork_oldest_pending_job_age_seconds",
        "thenetwork_primary_intake_paused",
        "thenetwork_control_actions_total",
        "thenetwork_agent_usage_limit_exceeded_total",
        "thenetwork_jobs_exhausted_total",
    }
    sample_names = {
        line.split()[0].split("{", 1)[0]
        for line in fixture.splitlines()
        if line and not line.startswith("#")
    }
    assert sample_names == expected_metrics


def test_metrics_configs_do_not_project_identifiers_into_labels():
    metrics_config = {
        "connector": _COLLECTOR_CONFIG["connectors"]["count/audit"],
        "prometheus": _PROMETHEUS_CONFIG,
    }
    serialized = yaml.safe_dump(metrics_config).lower()
    prohibited_labels = {
        "trace_id",
        "run_id",
        "sender_id_hash",
        "email",
        "content",
        "person_id",
        "event_id",
    }
    for label in prohibited_labels:
        assert f"{label}:" not in serialized
        assert f'["{label}"]' not in serialized


# ---------------------------------------------------------------------------
# Transform simulation tests
# ---------------------------------------------------------------------------


def test_audit_log_structured_processing():
    """Audit event records are parsed into structured attributes with body
    cleared."""
    audit_payload = {
        "event": "inbound_email_processed",
        "logger": "thenetwork.audit",
        "level": "info",
        "timestamp": "2026-07-22T04:05:00Z",
        "trace_id": "00000000000000000000000000000001",
        "pseudonym_id": "pseudo-99",
    }
    record = {
        "log": json.dumps(audit_payload) + "\n",
        "container_name": "/worker-1",
        "source": "stdout",
    }

    result = _process_json_log_record(record)

    # Parsed fields promoted to attributes
    assert result["event"] == "inbound_email_processed"
    assert result["logger"] == "thenetwork.audit"
    assert result["level"] == "info"
    assert result["trace_id"] == "00000000000000000000000000000001"
    assert result["pseudonym_id"] == "pseudo-99"
    # Docker metadata preserved
    assert result["container_name"] == "/worker-1"
    assert result["source"] == "stdout"
    # Body cleared after parse (matches OTTL: set(body, ""))
    assert result["body"] == ""


def test_procrastinate_log_structured_processing():
    """Procrastinate/foreign-logger records are parsed identically."""
    proc_payload = {
        "event": "job_completed",
        "logger": "procrastinate.worker",
        "level": "info",
        "timestamp": "2026-07-22T04:05:01Z",
        "job_id": 42,
        "queue": "default",
    }
    record = {
        "log": json.dumps(proc_payload) + "\n",
        "container_name": "/worker-1",
        "source": "stdout",
    }

    result = _process_json_log_record(record)

    assert result["event"] == "job_completed"
    assert result["logger"] == "procrastinate.worker"
    assert result["job_id"] == 42
    assert result["queue"] == "default"
    assert result["body"] == ""


def test_non_json_log_body_preserved():
    """Non-JSON log lines (e.g. raw stderr, stack traces) keep their body."""
    record = {
        "log": "Traceback (most recent call last):\n",
        "container_name": "/worker-1",
        "source": "stderr",
    }

    result = _process_json_log_record(record)

    # Body is NOT cleared for non-JSON
    assert result["body"] == "Traceback (most recent call last):\n"
    assert result["container_name"] == "/worker-1"
    # No parsed fields beyond Docker metadata
    assert "event" not in result


def test_exactly_once_output():
    """Each input record produces exactly one output record — no duplication
    from the transform pipeline."""
    records = [
        {
            "log": json.dumps({"event": "a", "logger": "x"}) + "\n",
            "container_name": "/w",
            "source": "stdout",
        },
        {
            "log": json.dumps({"event": "b", "logger": "y"}) + "\n",
            "container_name": "/w",
            "source": "stdout",
        },
    ]

    results = [_process_json_log_record(r) for r in records]
    assert len(results) == len(records)
    assert results[0]["event"] == "a"
    assert results[1]["event"] == "b"


def test_sensitive_fixture_values_absent():
    """Raw sensitive fixture values from audit test fixtures must not leak
    into processed output. The audit layer redacts before logging, so the
    collector should never see raw PII in the JSON body it processes."""
    # Simulating a properly redacted record (as the audit layer would emit)
    redacted_payload = {
        "event": "inbound_email_processed",
        "logger": "thenetwork.audit",
        "level": "info",
        "sender_pseudonym": "pseudo-abc",
        "body_chars": 142,
    }
    record = {
        "log": json.dumps(redacted_payload) + "\n",
        "container_name": "/worker-1",
        "source": "stdout",
    }

    result = _process_json_log_record(record)

    # The record contains pseudonyms, not raw email addresses
    assert result["sender_pseudonym"] == "pseudo-abc"
    # No raw email address or name appears
    for val in result.values():
        if isinstance(val, str):
            assert "@" not in val or val.startswith("pseudo"), (
                f"potential PII leak: {val}"
            )
