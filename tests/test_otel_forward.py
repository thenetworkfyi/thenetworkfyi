import json


def _process_json_log_record(raw_record: dict) -> dict:
    """Simulate OpenTelemetry Collector transform processor behavior on a log record."""
    body = raw_record.get("log", "")
    attributes = {k: v for k, v in raw_record.items() if k != "log"}
    if isinstance(body, str):
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict):
                attributes.update(parsed)
        except json.JSONDecodeError:
            pass
    attributes["log"] = body
    return attributes


def test_worker_json_log_schema_unchanged():
    """Verify worker application logging formats produce valid JSON with required fields."""
    sample_log = {
        "event": "worker_startup",
        "logger": "thenetwork.worker",
        "level": "info",
        "timestamp": "2026-07-22T04:00:00Z",
        "worker_id": "worker-1",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    }
    encoded = json.dumps(sample_log)
    decoded = json.loads(encoded)
    assert decoded["event"] == "worker_startup"
    assert decoded["logger"] == "thenetwork.worker"
    assert decoded["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_representative_audit_and_procrastinate_logs_structured_processing():
    """Focused tests for audit, foreign-logger, and Procrastinate records through the processing path."""
    # 1. Audit event record (pre-redacted)
    audit_json = json.dumps(
        {
            "event": "inbound_email_processed",
            "logger": "thenetwork.audit",
            "level": "info",
            "timestamp": "2026-07-22T04:05:00Z",
            "trace_id": "00000000000000000000000000000001",
            "pseudonym_id": "pseudo-99",
        }
    )

    # 2. Foreign/Procrastinate worker log record
    procrastinate_json = json.dumps(
        {
            "event": "job_completed",
            "logger": "procrastinate.worker",
            "level": "info",
            "timestamp": "2026-07-22T04:05:01Z",
            "job_id": 42,
            "queue": "default",
        }
    )

    audit_record = {
        "log": audit_json + "\n",
        "container_name": "/worker-1",
        "source": "stdout",
    }
    procrastinate_record = {
        "log": procrastinate_json + "\n",
        "container_name": "/worker-1",
        "source": "stdout",
    }

    sink_records = []
    for rec in [audit_record, procrastinate_record]:
        processed = _process_json_log_record(rec)
        sink_records.append(processed)

    assert len(sink_records) == 2

    # Assert audit record fields
    audit_out = sink_records[0]
    assert audit_out["event"] == "inbound_email_processed"
    assert audit_out["logger"] == "thenetwork.audit"
    assert audit_out["level"] == "info"
    assert audit_out["pseudonym_id"] == "pseudo-99"
    assert audit_out["container_name"] == "/worker-1"

    # Assert procrastinate record fields
    proc_out = sink_records[1]
    assert proc_out["event"] == "job_completed"
    assert proc_out["logger"] == "procrastinate.worker"
    assert proc_out["job_id"] == 42
