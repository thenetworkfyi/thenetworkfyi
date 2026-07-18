"""Tests for server-owned introduction relay addresses."""

from contextlib import contextmanager

import pytest

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.email.relay import (
    build_relay_address,
    parse_relay_address,
    resolve_relay_destination,
)


TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DOMAIN = "relay.example.com"


def test_build_relay_address_uses_fixed_format():
    assert build_relay_address(TOKEN, DOMAIN) == f"hidden-{TOKEN}@{DOMAIN}"


def test_parse_relay_address_returns_canonical_token():
    address = build_relay_address(TOKEN, DOMAIN)

    assert parse_relay_address(address, DOMAIN) == TOKEN
    assert parse_relay_address(address.replace(DOMAIN, DOMAIN.upper()), DOMAIN) == TOKEN


@pytest.mark.parametrize(
    "address",
    [
        f"visible-{TOKEN}@{DOMAIN}",
        f"hidden-not-a-uuid@{DOMAIN}",
        f"hidden-{TOKEN}@other.example.com",
        f"The Network <hidden-{TOKEN}@{DOMAIN}>",
        f"hidden-{TOKEN.upper()}@{DOMAIN}",
        f" hidden-{TOKEN}@{DOMAIN}",
    ],
)
def test_parse_relay_address_rejects_nonconfigured_formats(address):
    assert parse_relay_address(address, DOMAIN) is None


@pytest.mark.parametrize("domain", ["", "relay@example.com", "bad domain", ".bad"])
def test_build_relay_address_rejects_invalid_domain(domain):
    with pytest.raises(ValueError):
        build_relay_address(TOKEN, domain)


def test_settings_exposes_relay_domain(monkeypatch):
    from thenetwork.settings import Settings

    monkeypatch.setenv("RELAY_DOMAIN", DOMAIN)
    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
    )

    assert settings.relay_domain == DOMAIN


def test_settings_exposes_relay_imap_credentials(monkeypatch):
    from thenetwork.settings import Settings

    monkeypatch.setenv("RELAY_IMAP_ACCOUNT", "relay@relay.example.com")
    monkeypatch.setenv("RELAY_IMAP_PASSWORD", "relay-secret")
    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
    )

    assert settings.relay_imap_account == "relay@relay.example.com"
    assert settings.relay_imap_password == "relay-secret"


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Session:
    def __init__(self, consent, people):
        self.consent = consent
        self.people = people
        self.exec_calls = 0

    def exec(self, _query):
        self.exec_calls += 1
        if self.consent is None or self.consent.status != "introduced":
            return _Result(None)
        return _Result(self.consent)

    def get(self, _model, person_id):
        return self.people.get(person_id)


def _session_factory(session):
    @contextmanager
    def open_session():
        yield session

    return open_session


def _route_session(status="introduced"):
    consent = IntroductionConsent(
        person_a_id="alice",
        person_b_id="bob",
        reply_token=TOKEN,
        status=status,
    )
    people = {
        "alice": Person(id="alice", name="Alice", email="alice@example.com"),
        "bob": Person(id="bob", name="Bob", email="bob@example.com"),
    }
    return _Session(consent, people)


@pytest.mark.parametrize(
    ("sender", "destination"),
    [
        ("alice@example.com", "bob@example.com"),
        ("bob@example.com", "alice@example.com"),
    ],
)
def test_resolve_relay_destination_routes_only_to_opposite_participant(
    sender, destination
):
    session = _route_session()

    result = resolve_relay_destination(
        recipient_address=build_relay_address(TOKEN, DOMAIN),
        sender_email=sender,
        sender_authenticated=True,
        relay_domain=DOMAIN,
        session_factory=_session_factory(session),
    )

    assert result == destination


@pytest.mark.parametrize("status", ["proposed", "one_consented", "declined", "revoked"])
def test_resolve_relay_destination_rejects_nonintroduced_states(status):
    session = _route_session(status)

    result = resolve_relay_destination(
        recipient_address=build_relay_address(TOKEN, DOMAIN),
        sender_email="alice@example.com",
        sender_authenticated=True,
        relay_domain=DOMAIN,
        session_factory=_session_factory(session),
    )

    assert result is None


@pytest.mark.parametrize(
    ("sender", "authenticated"),
    [("mallory@example.com", True), ("alice@example.com", False)],
)
def test_resolve_relay_destination_rejects_unauthorized_sender(sender, authenticated):
    session = _route_session()

    result = resolve_relay_destination(
        recipient_address=build_relay_address(TOKEN, DOMAIN),
        sender_email=sender,
        sender_authenticated=authenticated,
        relay_domain=DOMAIN,
        session_factory=_session_factory(session),
    )

    assert result is None


@pytest.mark.parametrize(
    "recipient",
    [
        f"hidden-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb@{DOMAIN}",
        f"hidden-{TOKEN}@other.example.com",
        "not-a-relay-address",
    ],
)
def test_resolve_relay_destination_rejects_unknown_or_invalid_alias(recipient):
    session = _route_session()
    if TOKEN not in recipient:
        session.consent = None

    result = resolve_relay_destination(
        recipient_address=recipient,
        sender_email="alice@example.com",
        sender_authenticated=True,
        relay_domain=DOMAIN,
        session_factory=_session_factory(session),
    )

    assert result is None
