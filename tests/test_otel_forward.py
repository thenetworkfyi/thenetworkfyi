"""Tests for the OpenTelemetry Collector transform processor behavior.

Validates that the collector's transform pipeline (as configured in
otel-collector-config.yaml) correctly parses worker JSON log bodies into
structured LogRecord attributes while preserving the original readable body,
and that the count/audit connector derives the documented Prometheus counter
catalog from those attributes.

The config-shape assertions below run offline. The transform and count/audit
OTTL statements themselves are exercised against the real otelcol-contrib
binary in test_real_collector_transform_and_count_audit_connector, which is
the sole source of truth for OTTL correctness — there is no hand-rolled OTTL
interpreter here to keep in sync with the collector's actual behavior.
"""

import json
import pathlib
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest
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
_LOKI_CONFIG = yaml.safe_load((_REPO_ROOT / "loki-config.yaml").read_text())
_WORKER_METRIC_SOURCE_CONFIG = yaml.safe_load(
    (_REPO_ROOT / "tests/fixtures/otel-worker-metrics-source.yaml").read_text()
)
_COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.118.0"


# ---------------------------------------------------------------------------
# Config-level checks
# ---------------------------------------------------------------------------


def test_collector_config_parses_json_and_preserves_readable_body():
    """The transform extracts fields without blanking the Loki log line."""
    transform = _COLLECTOR_CONFIG.get("processors", {}).get("transform", {})
    stmts = transform.get("log_statements", [{}])[0].get("statements", [])
    assert any("ParseJSON" in s for s in stmts), (
        "transform processor must parse JSON body"
    )
    assert not any("set(body" in s for s in stmts), (
        "transform processor must preserve the original log body for Loki"
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


def test_collector_exports_logs_to_local_loki_without_external_settings():
    """The local monitoring stack starts without an external OTLP backend."""
    collector = _COMPOSE_CONFIG["services"]["otel-collector"]
    assert set(collector.get("environment", {})) == {
        "POSTGRES_DB",
        "POSTGRES_MONITOR_USER",
        "POSTGRES_MONITOR_PASSWORD",
    }, "collector must carry only the least-privilege postgresql receiver credentials"
    assert set(_COLLECTOR_CONFIG["exporters"]) == {
        "otlphttp/loki",
        "prometheus/audit",
        "prometheus/host",
    }
    assert _COLLECTOR_CONFIG["exporters"]["otlphttp/loki"]["endpoint"] == (
        "http://loki:3100/otlp"
    )
    assert _COLLECTOR_CONFIG["service"]["pipelines"]["logs"]["exporters"] == [
        "otlphttp/loki",
        "count/audit",
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

    rejection_condition = " ".join(
        count_config["thenetwork.messages.rejected"]["conditions"]
    )
    assert "intake|worker" in rejection_condition
    assert "cc_only_recipient" in rejection_condition

    pipelines = _COLLECTOR_CONFIG["service"]["pipelines"]
    assert pipelines["logs"]["exporters"] == ["otlphttp/loki", "count/audit"]
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


def test_prometheus_scrapes_collector_health_audit_activity_and_loki():
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
        "thenetwork-host-metrics": ["otel-collector:8890"],
        "loki": ["loki:3100"],
    }


def test_loki_service_is_pinned_private_persistent_and_retained():
    compose = _COMPOSE_CONFIG
    loki = compose["services"]["loki"]

    assert loki["image"] == "grafana/loki:3.6.11"
    assert loki["ports"] == ["127.0.0.1:${LOKI_HOST_PORT:-3100}:3100"]
    assert "loki-data:/loki" in loki["volumes"]
    assert "loki-data" in compose["volumes"]
    assert _LOKI_CONFIG["schema_config"]["configs"][0]["store"] == "tsdb"
    assert _LOKI_CONFIG["schema_config"]["configs"][0]["schema"] == "v13"
    assert _LOKI_CONFIG["limits_config"]["retention_period"] == "720h"
    assert _LOKI_CONFIG["compactor"]["retention_enabled"] is True
    assert _LOKI_CONFIG["compactor"]["delete_request_store"] == "filesystem"
    grafana = compose["services"]["grafana"]
    assert grafana["image"] == "grafana/grafana:11.5.0"
    assert grafana["ports"] == ["127.0.0.1:${GRAFANA_HOST_PORT:-3000}:3000"]
    assert (
        "./grafana/provisioning/datasources/datasources.yaml:/etc/grafana/provisioning/datasources/datasources.yaml:ro"
        in grafana["volumes"]
    )


def test_loki_uses_one_static_service_label_and_structured_metadata():
    resource = _COLLECTOR_CONFIG["processors"]["resource/loki"]
    assert resource == {
        "attributes": [
            {
                "key": "service.name",
                "value": "thenetwork-worker",
                "action": "upsert",
            }
        ]
    }
    assert _LOKI_CONFIG["limits_config"]["allow_structured_metadata"] is True


def test_prometheus_service_is_pinned_private_and_persistent():
    compose = _COMPOSE_CONFIG
    prometheus = compose["services"]["prometheus"]

    assert prometheus["image"] == "prom/prometheus:v3.5.5"
    assert prometheus["ports"] == ["127.0.0.1:${PROMETHEUS_HOST_PORT:-9090}:9090"]
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
# Real-collector smoke test
#
# Runs the actual otelcol-contrib binary against the checked-in transform
# processor and count/audit connector definitions (copied verbatim from
# otel-collector-config.yaml), feeds one sample log through the real
# fluentforward receiver, and reads the derived counter back from the real
# prometheus exporter. This replaces the former hand-rolled Python OTTL
# interpreter: OTTL correctness is now proven against the real engine
# instead of a reimplementation that could silently drift from it.
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
    except Exception:
        return False
    return True


def _msgpack_str(value: str) -> bytes:
    encoded = value.encode("utf-8")
    length = len(encoded)
    if length < 32:
        return bytes([0xA0 | length]) + encoded
    return b"\xda" + length.to_bytes(2, "big") + encoded


def _msgpack_uint32(value: int) -> bytes:
    return b"\xce" + value.to_bytes(4, "big")


def _msgpack_map(mapping: dict) -> bytes:
    header = bytes([0x80 | len(mapping)])
    body = b"".join(_msgpack_str(k) + _msgpack_str(v) for k, v in mapping.items())
    return header + body


def _msgpack_array(items: list) -> bytes:
    header = bytes([0x90 | len(items)])
    return header + b"".join(items)


def _fluent_forward_message(tag: str, record: dict) -> bytes:
    """Encode one Fluent Forward "Message Mode" entry: [tag, time, record]."""
    return _msgpack_array(
        [_msgpack_str(tag), _msgpack_uint32(int(time.time())), _msgpack_map(record)]
    )


def _published_port(container: str, container_port: str) -> int:
    output = subprocess.run(
        ["docker", "port", container, container_port],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    return int(output.rsplit(":", 1)[-1])


def _wait_for_health(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=1
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise AssertionError(f"collector never became healthy: {last_error}")


def _wait_for_metric_line(port: int, metric_name: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=1
            ) as response:
                text = response.read().decode()
        except (urllib.error.URLError, OSError):
            text = ""
        for line in text.splitlines():
            if line.startswith(metric_name):
                return line
        time.sleep(0.5)
    raise AssertionError(f"{metric_name} never appeared; last scrape:\n{text}")


@pytest.mark.integration
def test_real_collector_transform_and_count_audit_connector(tmp_path):
    """Feed a sample audit log through the real otelcol-contrib binary and
    confirm it emits the expected derived Prometheus counter.

    Builds a minimal config that reuses the checked-in transform processor
    and count/audit connector verbatim, so a config drift that would break
    production also breaks this test.
    """
    if not _docker_available():
        pytest.skip("docker is not available in this environment")

    smoke_config = {
        "receivers": {"fluentforward": {"endpoint": "0.0.0.0:24224"}},
        "processors": {"transform": _COLLECTOR_CONFIG["processors"]["transform"]},
        "connectors": {"count/audit": _COLLECTOR_CONFIG["connectors"]["count/audit"]},
        "exporters": {"prometheus/audit": {"endpoint": "0.0.0.0:8889"}},
        "extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}},
        "service": {
            "extensions": ["health_check"],
            "pipelines": {
                "logs": {
                    "receivers": ["fluentforward"],
                    "processors": ["transform"],
                    "exporters": ["count/audit"],
                },
                "metrics/audit": {
                    "receivers": ["count/audit"],
                    "exporters": ["prometheus/audit"],
                },
            },
        },
    }
    config_path = tmp_path / "smoke-config.yaml"
    config_path.write_text(yaml.safe_dump(smoke_config))

    container = f"otelcol-smoke-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-p",
            "127.0.0.1::24224",
            "-p",
            "127.0.0.1::8889",
            "-p",
            "127.0.0.1::13133",
            "-v",
            f"{config_path}:/etc/otelcol-contrib/config.yaml:ro",
            _COLLECTOR_IMAGE,
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:
        forward_port = _published_port(container, "24224/tcp")
        health_port = _published_port(container, "13133/tcp")
        metrics_port = _published_port(container, "8889/tcp")
        _wait_for_health(health_port)

        sample_log = json.dumps(
            {
                "logger": "thenetwork.audit",
                "event": "database.action",
                "action": "insert",
                "record_type": "person",
                "outcome": "success",
            }
        )
        with socket.create_connection(("127.0.0.1", forward_port), timeout=5) as sock:
            sock.sendall(
                _fluent_forward_message(
                    "docker.worker",
                    {"log": sample_log, "container_name": "/worker-1"},
                )
            )

        line = _wait_for_metric_line(metrics_port, "thenetwork_accounts_created_total")
        assert line.split()[-1] == "1"
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
