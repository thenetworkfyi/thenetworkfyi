import re

import pytest
from pydantic import ValidationError

from thenetwork.security.intake_fingerprint import intake_fingerprints
from thenetwork.security.sender_identifier import SenderIdentifierSecretMissing
from thenetwork.settings import Settings


def test_intake_fingerprints_are_stable_bounded_and_non_reversible():
    raw = "Alice.Private@Example.com"
    body = "Private campaign content"

    first = intake_fingerprints(raw, body, secret="monitor-secret")
    second = intake_fingerprints(
        "Alice Private <alice.private@example.com>",
        body,
        secret="monitor-secret",
    )

    assert first == second
    assert len(set(first)) == 3
    for value in first:
        assert re.fullmatch(r"intake_(sender|domain|body)_v1_[A-Za-z0-9_-]{22}", value)
        assert "alice" not in value.lower()
        assert "example" not in value.lower()
        assert "campaign" not in value.lower()


def test_intake_fingerprints_depend_on_secret_and_input_kind():
    first = intake_fingerprints("a@example.com", "body", secret="first")
    second = intake_fingerprints("a@example.com", "body", secret="second")

    assert first != second
    assert len(set(first)) == 3


def test_intake_fingerprints_require_secret():
    with pytest.raises(SenderIdentifierSecretMissing):
        intake_fingerprints("a@example.com", "body", secret="")


def test_enabling_burst_monitor_requires_sender_identifier_secret():
    with pytest.raises(ValidationError, match="SENDER_IDENTIFIER_SECRET is required"):
        Settings(
            agent_model="test:model",
            small_agent_model="test:model",
            embed_model="test:embed",
            primary_intake_burst_monitoring_enabled=True,
            sender_identifier_secret="",
        )

    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
        primary_intake_burst_monitoring_enabled=True,
        sender_identifier_secret="monitor-secret",
    )
    assert settings.primary_intake_burst_monitoring_enabled is True
