"""Producer coverage for primary and relay IMAP mailbox coordination."""

from unittest.mock import call, patch

import pytest

from thenetwork.email.inbound import InboundMessage
from thenetwork.worker.producer import (
    _poll_and_enqueue,
    _poll_mailbox_and_enqueue,
)


@pytest.fixture(autouse=True)
def _active_primary_intake(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.worker.producer.is_primary_intake_paused", lambda: False
    )


def _message(uid: str, sender: str) -> InboundMessage:
    return InboundMessage(
        uid=uid,
        sender=sender,
        subject="Relay mailbox test",
        body="Mailbox-specific delivery",
        auto_submitted=None,
        sender_authenticated=True,
    )


def test_poll_and_enqueue_processes_and_marks_each_configured_mailbox():
    messages = {
        "primary": [_message("1", "primary-sender@example.com")],
        "relay": [_message("1", "relay-sender@example.com")],
    }

    with (
        patch(
            "thenetwork.worker.producer.poll_unseen",
            side_effect=lambda *, mailbox: messages[mailbox],
        ) as poll_unseen,
        patch(
            "thenetwork.worker.producer.relay_mailbox_configured",
            return_value=True,
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_and_enqueue() == 2

    assert poll_unseen.call_args_list == [
        call(mailbox="primary"),
        call(mailbox="relay"),
    ]
    assert process_email.defer.call_count == 2
    assert mark_seen.call_args_list == [
        call(["1"], mailbox="primary"),
        call(["1"], mailbox="relay"),
    ]


def test_poll_and_enqueue_skips_unconfigured_relay_mailbox():
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[]) as poll_unseen,
        patch(
            "thenetwork.worker.producer.relay_mailbox_configured",
            return_value=False,
        ),
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_and_enqueue() == 0

    poll_unseen.assert_called_once_with(mailbox="primary")
    mark_seen.assert_called_once_with([], mailbox="primary")


@pytest.mark.parametrize("mailbox", ["primary", "relay"])
def test_disposable_sender_is_rejected_before_enqueue(mailbox):
    message = _message("7", "sender@mailinator.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
        patch("thenetwork.worker.producer.mark_message_processed") as mark_processed,
    ):
        assert _poll_mailbox_and_enqueue(mailbox) == 0

    process_email.defer.assert_not_called()
    mark_processed.assert_not_called()
    mark_seen.assert_called_once_with(["7"], mailbox=mailbox)


@pytest.mark.parametrize("domain", ["gmail.com", "outlook.com", "proton.me"])
def test_established_provider_is_accepted(domain):
    message = _message("8", f"sender@{domain}")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 1

    process_email.defer.assert_called_once()
    mark_seen.assert_called_once_with(["8"], mailbox="primary")


def test_paused_primary_leaves_ordinary_messages_unread_and_unenqueued():
    message = _message("9", "sender@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary", primary_paused=True) == 0

    process_email.defer.assert_not_called()
    mark_seen.assert_called_once_with([], mailbox="primary")


def test_paused_primary_enqueues_admin_candidate_for_pgp_verification():
    message = _message("10", "admin@gmail.com")
    message.subject = "ADMIN: resume-intake"
    message.raw_message = b"signed candidate"

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary", primary_paused=True) == 1

    assert process_email.defer.call_args.kwargs["source_mailbox"] == "primary"
    mark_seen.assert_called_once_with(["10"], mailbox="primary")


def test_paused_primary_does_not_block_relay_candidate():
    message = _message("11", "member@gmail.com")
    message.recipient_address = "hidden-token@relay.example.com"

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer._is_relay_candidate", return_value=True),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary", primary_paused=True) == 1

    process_email.defer.assert_called_once()
    mark_seen.assert_called_once_with(["11"], mailbox="primary")


def test_pause_never_applies_to_separate_relay_mailbox():
    message = _message("12", "member@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("relay", primary_paused=True) == 1

    assert process_email.defer.call_args.kwargs["source_mailbox"] == "relay"
    mark_seen.assert_called_once_with(["12"], mailbox="relay")
