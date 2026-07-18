"""Producer coverage for primary and relay IMAP mailbox coordination."""

from unittest.mock import call, patch

from thenetwork.email.inbound import InboundMessage
from thenetwork.worker.producer import _poll_and_enqueue


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
