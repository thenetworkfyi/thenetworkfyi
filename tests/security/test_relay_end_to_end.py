"""End-to-end security contracts for hidden introduction email replies."""

from __future__ import annotations

from contextlib import contextmanager
from email.utils import getaddresses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.db.models import IntroductionConsent, Person


TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DOMAIN = "relay.example.com"
PROXY = f"hidden-{TOKEN}@{DOMAIN}"


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _RelaySession:
    def __init__(self, *, status="introduced", token_known=True):
        self.consent = (
            IntroductionConsent(
                person_a_id="alice",
                person_b_id="bob",
                reply_token=TOKEN,
                status=status,
            )
            if token_known
            else None
        )
        self.people = {
            "alice": Person(
                id="alice", name="Alice", email="alice.private@example.com"
            ),
            "bob": Person(id="bob", name="Bob", email="bob.private@example.com"),
        }

    def exec(self, _query):
        if self.consent is None or self.consent.status != "introduced":
            return _Result(None)
        return _Result(self.consent)

    def get(self, model, identity):
        if model is Person:
            return self.people.get(identity)
        return None


def _session_factory(session):
    @contextmanager
    def open_session():
        yield session

    return open_session


def _worker_session():
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.get.return_value = None
    return session


def _smtp_capture():
    captured = []
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    smtp.send_message.side_effect = captured.append
    return smtp, captured


def _settings():
    return SimpleNamespace(
        relay_domain=DOMAIN,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_account="ses-user",
        smtp_password="secret",
    )


@pytest.mark.parametrize(
    ("sender", "destination"),
    [
        ("alice.private@example.com", "bob.private@example.com"),
        ("bob.private@example.com", "alice.private@example.com"),
    ],
)
@pytest.mark.asyncio
async def test_introduced_pair_relay_is_server_routed_without_agent(
    sender, destination
):
    from thenetwork.worker.tasks import process_email

    relay_session = _RelaySession()
    smtp, captured = _smtp_capture()
    agent = AsyncMock()
    scan = MagicMock()
    consent = MagicMock()
    body = "First unchanged line\nSecond unchanged line"

    with (
        patch("thenetwork.worker.tasks.get_settings", return_value=_settings()),
        patch("thenetwork.email.outbound.get_settings", return_value=_settings()),
        patch("thenetwork.worker.tasks.get_session", return_value=_worker_session()),
        patch(
            "thenetwork.email.relay.get_session",
            _session_factory(relay_session),
        ),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", scan),
        patch("thenetwork.worker.tasks.process_consent_reply", consent),
        patch("thenetwork.worker.tasks.run_agent_for_email", agent),
        patch("thenetwork.email.outbound.smtplib.SMTP", return_value=smtp),
        patch("thenetwork.email.outbound._append_to_sent"),
    ):
        await process_email.func(
            sender_email=sender,
            sender_authenticated=True,
            recipient_address=PROXY,
            subject="Re: Your introduction",
            body=body,
        )

    assert len(captured) == 1
    message = captured[0]
    assert str(message["From"]) == f"The Network <{PROXY}>"
    assert str(message["Reply-To"]) == PROXY
    assert getaddresses(message.get_all("To", [])) == [("", destination)]
    assert str(message["Subject"]) == "Re: Your introduction"
    assert message.get_content().rstrip("\n") == body
    assert not message.is_multipart()
    header_blob = "\n".join(str(value) for value in message.values())
    assert sender not in header_blob
    assert "Alice" not in str(message["From"])
    assert "Bob" not in str(message["From"])
    scan.assert_not_called()
    consent.assert_not_called()
    agent.assert_not_awaited()


@pytest.mark.parametrize(
    ("recipient", "sender", "authenticated", "status", "token_known"),
    [
        (PROXY, "mallory@example.com", True, "introduced", True),
        (PROXY, "alice.private@example.com", False, "introduced", True),
        (PROXY, "alice.private@example.com", True, "proposed", True),
        (PROXY, "alice.private@example.com", True, "one_consented", True),
        (PROXY, "alice.private@example.com", True, "declined", True),
        (PROXY, "alice.private@example.com", True, "revoked", True),
        (PROXY, "alice.private@example.com", True, "introduced", False),
        (
            f"hidden-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb@{DOMAIN}",
            "alice.private@example.com",
            True,
            "introduced",
            False,
        ),
        (
            f"hidden-not-a-token@{DOMAIN}",
            "alice.private@example.com",
            True,
            "introduced",
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_relay_attempts_send_nothing_and_never_run_agent(
    recipient, sender, authenticated, status, token_known
):
    from thenetwork.worker.tasks import process_email

    smtp, captured = _smtp_capture()
    agent = AsyncMock()
    with (
        patch("thenetwork.worker.tasks.get_settings", return_value=_settings()),
        patch("thenetwork.email.outbound.get_settings", return_value=_settings()),
        patch("thenetwork.worker.tasks.get_session", return_value=_worker_session()),
        patch(
            "thenetwork.email.relay.get_session",
            _session_factory(_RelaySession(status=status, token_known=token_known)),
        ),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content") as scan,
        patch("thenetwork.worker.tasks.process_consent_reply") as consent,
        patch("thenetwork.worker.tasks.run_agent_for_email", agent),
        patch("thenetwork.email.outbound.smtplib.SMTP", return_value=smtp),
        patch("thenetwork.email.outbound._append_to_sent"),
    ):
        await process_email.func(
            sender_email=sender,
            sender_authenticated=authenticated,
            recipient_address=recipient,
            subject="Relay attempt",
            body="This must not leave the server",
        )

    assert captured == []
    scan.assert_not_called()
    consent.assert_not_called()
    agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_mail_recipient_still_runs_existing_agent_path():
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.worker.tasks import process_email

    session = _worker_session()
    session.exec.return_value.first.return_value = "alice"
    agent = AsyncMock()
    with (
        patch("thenetwork.worker.tasks.get_settings", return_value=_settings()),
        patch("thenetwork.worker.tasks.get_session", return_value=session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=ConsentReplyResult(handled=False),
        ),
        patch("thenetwork.worker.tasks.record_sent_email_memories", AsyncMock()),
        patch("thenetwork.worker.tasks.run_agent_for_email", agent),
        patch("thenetwork.worker.tasks.send_relay_email") as relay_send,
    ):
        await process_email.func(
            sender_email="alice.private@example.com",
            sender_authenticated=True,
            recipient_address="join@example.com",
            subject="Ordinary subject",
            body="Ordinary body remains on the agent path",
        )

    relay_send.assert_not_called()
    agent.assert_awaited_once()
    assert agent.await_args.kwargs["email_subject"] == "Ordinary subject"
    assert agent.await_args.kwargs["email_body"] == (
        "Ordinary body remains on the agent path"
    )
