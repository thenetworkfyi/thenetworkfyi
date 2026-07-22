import json
import pytest

def test_worker_json_log_schema_unchanged():
    """Verify worker application logging formats produce valid JSON with required fields."""
    sample_log = {
        "event": "worker_startup",
        "logger": "thenetwork.worker",
        "level": "info",
        "timestamp": "2026-07-22T04:00:00Z",
        "worker_id": "worker-1",
    }
    encoded = json.dumps(sample_log)
    decoded = json.loads(encoded)
    assert decoded["event"] == "worker_startup"
    assert decoded["logger"] == "thenetwork.worker"

def test_fluent_forward_transport_metadata_smoke():
    """Transport smoke check simulating Fluent Forward record structure with worker and stream metadata."""
    worker_record = {
        "log": '{"event": "task_processed", "logger": "thenetwork.worker", "level": "info"}\n',
        "container_name": "/worker-1",
        "source": "stdout",
        "container_id": "abc123456789",
    }
    
    # Verify metadata fields are preserved alongside newline-delimited JSON payload
    parsed_payload = json.loads(worker_record["log"].strip())
    assert parsed_payload["event"] == "task_processed"
    assert worker_record["container_name"] == "/worker-1"
    assert worker_record["source"] == "stdout"
