"""Security contracts for server-side introduction relay resolution."""

from contextlib import contextmanager
from unittest.mock import MagicMock

from thenetwork.email.relay import resolve_relay_destination


TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RECIPIENT = f"hidden-{TOKEN}@relay.example.com"


def test_unauthenticated_relay_sender_cannot_trigger_database_lookup():
    session_factory = MagicMock()

    destination = resolve_relay_destination(
        recipient_address=RECIPIENT,
        sender_email="alice@example.com",
        sender_authenticated=False,
        relay_domain="relay.example.com",
        session_factory=session_factory,
    )

    assert destination is None
    session_factory.assert_not_called()


def test_unknown_relay_token_returns_no_participant_address():
    session = MagicMock()
    session.exec.return_value.first.return_value = None

    @contextmanager
    def session_factory():
        yield session

    destination = resolve_relay_destination(
        recipient_address=RECIPIENT,
        sender_email="mallory@example.com",
        sender_authenticated=True,
        relay_domain="relay.example.com",
        session_factory=session_factory,
    )

    assert destination is None
    session.get.assert_not_called()
