from __future__ import annotations

import re

import pytest

from thenetwork.security.sender_identifier import (
    SenderIdentifierSecretMissing,
    normalize_sender_identifier_identity,
    optional_sender_identifier,
    sender_identifier,
)
from thenetwork.settings import Settings


def test_sender_identifier_is_stable_for_same_sender_and_secret():
    first = sender_identifier(" Alice <Alice.Private+tag@Example.COM> ", secret="audit-secret")
    second = sender_identifier("alice.private+tag@example.com", secret="audit-secret")

    assert first == second


def test_sender_identifier_differs_for_different_senders():
    identifiers = {
        sender_identifier(f"person-{index}@example.com", secret="audit-secret")
        for index in range(200)
    }

    assert len(identifiers) == 200


def test_sender_identifier_depends_on_server_secret():
    sender = "alice.private@example.com"

    assert sender_identifier(sender, secret="first-secret") != sender_identifier(
        sender,
        secret="second-secret",
    )


def test_sender_identifier_is_bounded_and_does_not_contain_raw_email():
    raw_sender = "alice.private@example.com"
    identifier = sender_identifier(raw_sender, secret="audit-secret")

    assert re.fullmatch(r"snd_v1_[A-Za-z0-9_-]{22}", identifier)
    assert "alice" not in identifier
    assert "example" not in identifier
    assert "@" not in identifier


def test_sender_identifier_rejects_missing_secret():
    with pytest.raises(SenderIdentifierSecretMissing):
        sender_identifier("alice@example.com", secret="")


def test_normalize_sender_identifier_identity_keeps_provider_aliases_literal():
    assert (
        normalize_sender_identifier_identity("Alice+one@GMAIL.com")
        == "alice+one@gmail.com"
    )
    assert (
        normalize_sender_identifier_identity("alice+two@gmail.com")
        == "alice+two@gmail.com"
    )


def test_sender_identifier_secret_is_configurable():
    settings = Settings(sender_identifier_secret="audit-secret")

    assert settings.sender_identifier_secret == "audit-secret"


def test_optional_sender_identifier_omits_when_secret_missing(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.security.sender_identifier.get_settings",
        lambda: Settings(sender_identifier_secret=""),
    )

    assert optional_sender_identifier("alice@example.com") is None
