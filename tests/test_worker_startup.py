from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


def test_worker_main_checks_presidio_before_run_loop(monkeypatch):
    from thenetwork.worker import tasks
    from thenetwork.embed import embeddings
    from thenetwork.security import content_scan

    events: list[str] = []

    async def fake_run_worker() -> None:
        events.append("run_worker")

    monkeypatch.setattr(
        embeddings,
        "validate_embedding_configuration",
        lambda: events.append("embedding_validation"),
    )
    monkeypatch.setattr(
        tasks, "configure_audit_logging", lambda: events.append("audit")
    )
    monkeypatch.setattr(
        tasks, "assert_presidio_ready", lambda: events.append("presidio")
    )
    monkeypatch.setattr(
        content_scan,
        "assert_content_scanner_ready",
        lambda: events.append("content_scanner"),
    )
    monkeypatch.setattr(tasks, "run_worker", fake_run_worker)

    tasks.main()

    assert events == [
        "embedding_validation",
        "audit",
        "presidio",
        "content_scanner",
        "run_worker",
    ]


def test_worker_main_fails_before_run_loop_when_presidio_unavailable(monkeypatch):
    from thenetwork.worker import tasks
    from thenetwork.embed import embeddings

    run_worker = AsyncMock()

    def fail_presidio() -> None:
        raise RuntimeError("Presidio AnalyzerEngine could not start")

    monkeypatch.setattr(embeddings, "validate_embedding_configuration", lambda: None)
    monkeypatch.setattr(tasks, "configure_audit_logging", lambda: None)
    monkeypatch.setattr(tasks, "assert_presidio_ready", fail_presidio)
    monkeypatch.setattr(tasks, "run_worker", run_worker)

    with pytest.raises(RuntimeError, match="Presidio AnalyzerEngine could not start"):
        tasks.main()

    run_worker.assert_not_called()
