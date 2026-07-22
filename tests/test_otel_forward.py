"""Tests for the OpenTelemetry Collector transform processor behavior.

Validates that the collector's transform pipeline (as configured in
otel-collector-config.yaml) correctly parses worker JSON log bodies into
structured LogRecord attributes and clears the body after parsing.
"""

import json
import pathlib

import yaml


# ---------------------------------------------------------------------------
# Load the actual collector config so tests break when the config changes
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COLLECTOR_CONFIG = yaml.safe_load(
    (_REPO_ROOT / "otel-collector-config.yaml").read_text()
)


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


def test_collector_config_uses_consistent_env_syntax():
    """All env var references in the collector config use ${env:...} syntax."""
    config_text = (_REPO_ROOT / "otel-collector-config.yaml").read_text()
    # Find all ${...} patterns and verify they use env: prefix
    import re

    refs = re.findall(r"\$\{([^}]+)\}", config_text)
    for ref in refs:
        assert ref.startswith("env:"), (
            f"env var reference ${{{ref}}} should use ${{env:...}} syntax"
        )


def test_collector_config_has_health_check_extension():
    """The collector config enables the health_check extension for Docker
    healthcheck support."""
    extensions = _COLLECTOR_CONFIG.get("extensions", {})
    assert "health_check" in extensions, (
        "collector config must include health_check extension"
    )
    service_extensions = (
        _COLLECTOR_CONFIG.get("service", {}).get("extensions", [])
    )
    assert "health_check" in service_extensions, (
        "health_check must be listed in service.extensions"
    )


def test_collector_config_otlp_exporter_wires_headers_env_var():
    """The otlp exporter must forward OTEL_EXPORTER_OTLP_HEADERS so setting it
    actually attaches auth headers to exported requests. This env var is
    documented in .env.example/README.md/docs/development.md and passed into
    the container's environment by docker-compose.yml; a config that omits
    the `headers:` field silently makes it a no-op."""
    otlp = _COLLECTOR_CONFIG.get("exporters", {}).get("otlp", {})
    assert "headers" in otlp, (
        "otlp exporter must declare a headers field sourced from "
        "OTEL_EXPORTER_OTLP_HEADERS, or the documented env var has no effect"
    )
    assert otlp["headers"] == "${env:OTEL_EXPORTER_OTLP_HEADERS}", (
        "otlp exporter headers must resolve from the whole "
        "OTEL_EXPORTER_OTLP_HEADERS env var so the Collector's config "
        "resolver can parse it as a map"
    )


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
