"""Producer coverage for primary and relay IMAP mailbox coordination."""

from unittest.mock import call, patch
from types import SimpleNamespace

import pytest

from thenetwork.email.inbound import InboundMessage
from thenetwork.email.intake_observations import BurstObservationResult
from thenetwork.worker.producer import (
    _poll_and_enqueue,
    _poll_mailbox_and_enqueue,
)


@pytest.fixture(autouse=True)
def _active_primary_intake(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.worker.producer.is_primary_intake_paused", lambda: False
    )
    monkeypatch.setattr(
        "thenetwork.worker.producer.get_settings",
        lambda: SimpleNamespace(
            primary_intake_burst_monitoring_enabled=False,
            sender_identifier_secret="",
            relay_domain="relay.example.com",
            daily_agent_token_cap=0,
        ),
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

    assert isinstance(
        process_email.defer.call_args.kwargs["intake_observed_at_epoch_seconds"],
        float,
    )

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


def test_over_budget_primary_leaves_ordinary_messages_unread_and_unenqueued():
    message = _message("30", "sender@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ),
        patch(
            "thenetwork.worker.producer._is_known_authenticated_sender",
            return_value=False,
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
        patch("thenetwork.worker.producer.mark_message_processed") as mark_processed,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

    process_email.defer.assert_not_called()
    mark_processed.assert_not_called()
    mark_seen.assert_called_once_with([], mailbox="primary")


def test_over_budget_primary_enqueues_admin_candidate_for_pgp_verification():
    message = _message("31", "admin@gmail.com")
    message.subject = "ADMIN: intake-status"
    message.raw_message = b"signed candidate"

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 1

    assert process_email.defer.call_args.kwargs["source_mailbox"] == "primary"
    mark_seen.assert_called_once_with(["31"], mailbox="primary")


def test_over_budget_primary_does_not_block_relay_candidate():
    message = _message("32", "member@gmail.com")
    message.recipient_address = "hidden-token@relay.example.com"

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer._is_relay_candidate", return_value=True),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 1

    process_email.defer.assert_called_once()
    mark_seen.assert_called_once_with(["32"], mailbox="primary")


def test_over_budget_never_applies_to_separate_relay_mailbox():
    message = _message("33", "member@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ) as budget_check,
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("relay") == 1

    budget_check.assert_not_called()
    assert process_email.defer.call_args.kwargs["source_mailbox"] == "relay"
    mark_seen.assert_called_once_with(["33"], mailbox="relay")


def test_over_budget_notifies_known_authenticated_sender_once():
    message = _message("34", "known@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ),
        patch(
            "thenetwork.worker.producer._is_known_authenticated_sender",
            return_value=True,
        ),
        patch(
            "thenetwork.worker.producer.should_send_deferral_notice",
            return_value=True,
        ) as should_notify,
        patch(
            "thenetwork.worker.producer._send_infrastructure_rejection_reply"
        ) as send_notice,
        patch("thenetwork.worker.producer.process_email"),
        patch("thenetwork.worker.producer.mark_messages_seen"),
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

    should_notify.assert_called_once_with("known@gmail.com")
    send_notice.assert_called_once()
    assert send_notice.call_args.kwargs["sender_email"] == "known@gmail.com"
    assert send_notice.call_args.kwargs["reason"] == "daily_token_budget_exhausted"


def test_over_budget_does_not_notify_unknown_sender():
    message = _message("35", "unknown@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ),
        patch(
            "thenetwork.worker.producer._is_known_authenticated_sender",
            return_value=False,
        ),
        patch(
            "thenetwork.worker.producer.should_send_deferral_notice"
        ) as should_notify,
        patch(
            "thenetwork.worker.producer._send_infrastructure_rejection_reply"
        ) as send_notice,
        patch("thenetwork.worker.producer.process_email"),
        patch("thenetwork.worker.producer.mark_messages_seen"),
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

    should_notify.assert_not_called()
    send_notice.assert_not_called()


def test_over_budget_does_not_renotify_within_the_same_day():
    message = _message("36", "known@gmail.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer.check_daily_token_budget", return_value=False
        ),
        patch(
            "thenetwork.worker.producer._is_known_authenticated_sender",
            return_value=True,
        ),
        patch(
            "thenetwork.worker.producer.should_send_deferral_notice",
            return_value=False,
        ),
        patch(
            "thenetwork.worker.producer._send_infrastructure_rejection_reply"
        ) as send_notice,
        patch("thenetwork.worker.producer.process_email"),
        patch("thenetwork.worker.producer.mark_messages_seen"),
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

    send_notice.assert_not_called()


@pytest.mark.integration
def test_over_budget_message_is_enqueued_after_the_daily_window_rolls(
    pg_engine, monkeypatch
):
    """A message deferred while the daily token budget is exhausted must be
    enqueued by a later poll once the day's fixed window resets - proving
    genuine next-day pickup rather than the message being lost."""
    from sqlalchemy import text

    import thenetwork.db.session as sess_mod
    import thenetwork.security.token_budget as token_budget

    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    token_budget._limiter = None
    token_budget._storage = None
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits"))

    monkeypatch.setattr(
        "thenetwork.worker.producer.get_settings",
        lambda: SimpleNamespace(
            primary_intake_burst_monitoring_enabled=False,
            sender_identifier_secret="",
            relay_domain="relay.example.com",
            daily_agent_token_cap=10,
        ),
    )
    # Exhaust today's budget the same way the observability path would.
    assert token_budget.consume_daily_token_budget(10, 10) is True

    message = _message("40", "sender@gmail.com")
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch(
            "thenetwork.worker.producer._is_known_authenticated_sender",
            return_value=False,
        ),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0
    process_email.defer.assert_not_called()
    mark_seen.assert_called_once_with([], mailbox="primary")

    # Simulate the day's fixed window rolling over.
    with pg_engine.begin() as conn:
        conn.execute(
            text("UPDATE rate_limits SET expires_at = now() - INTERVAL '1 second'")
        )

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 1
    process_email.defer.assert_called_once()
    mark_seen.assert_called_once_with(["40"], mailbox="primary")

    token_budget._limiter = None
    token_budget._storage = None


def test_new_sender_burst_pauses_before_enqueue_and_leaves_batch_unread(monkeypatch):
    messages = [
        _message(str(index), f"sender-{index}@example.com") for index in range(25)
    ]
    monkeypatch.setattr(
        "thenetwork.worker.producer.get_settings",
        lambda: SimpleNamespace(
            primary_intake_burst_monitoring_enabled=True,
            sender_identifier_secret="monitor-secret",
            relay_domain="relay.example.com",
            daily_agent_token_cap=0,
        ),
    )

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=messages),
        patch(
            "thenetwork.worker.producer.observe_primary_intake_batch",
            return_value=BurstObservationResult(
                paused=True,
                newly_observed=25,
                distinct_new_senders=25,
            ),
        ) as observe,
        patch("thenetwork.worker.producer.process_email") as process_email,
        patch("thenetwork.worker.producer.mark_messages_seen") as mark_seen,
        patch("thenetwork.worker.producer.mark_message_processed") as mark_processed,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

    observe.assert_called_once_with(messages, secret="monitor-secret")
    process_email.defer.assert_not_called()
    mark_processed.assert_not_called()
    mark_seen.assert_called_once_with([], mailbox="primary")


def test_monitor_excludes_admin_and_relay_candidates(monkeypatch):
    ordinary = _message("20", "ordinary@example.com")
    admin = _message("21", "admin@example.com")
    admin.subject = "ADMIN: intake-status"
    admin.raw_message = b"signed candidate"
    relay = _message("22", "member@example.com")
    relay.recipient_address = "hidden-token@relay.example.com"
    monkeypatch.setattr(
        "thenetwork.worker.producer.get_settings",
        lambda: SimpleNamespace(
            primary_intake_burst_monitoring_enabled=True,
            sender_identifier_secret="monitor-secret",
            relay_domain="relay.example.com",
            daily_agent_token_cap=0,
        ),
    )

    with (
        patch(
            "thenetwork.worker.producer.poll_unseen",
            return_value=[ordinary, admin, relay],
        ),
        patch(
            "thenetwork.worker.producer._is_relay_candidate",
            side_effect=lambda address: bool(address),
        ),
        patch(
            "thenetwork.worker.producer.observe_primary_intake_batch",
            return_value=BurstObservationResult(
                paused=False,
                newly_observed=1,
                distinct_new_senders=1,
            ),
        ) as observe,
        patch("thenetwork.worker.producer.process_email"),
        patch("thenetwork.worker.producer.mark_messages_seen"),
    ):
        assert _poll_mailbox_and_enqueue("primary") == 3

    observe.assert_called_once_with([ordinary], secret="monitor-secret")


def test_relay_mailbox_is_never_observed(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.worker.producer.get_settings",
        lambda: SimpleNamespace(
            primary_intake_burst_monitoring_enabled=True,
            sender_identifier_secret="monitor-secret",
            relay_domain="relay.example.com",
            daily_agent_token_cap=0,
        ),
    )
    message = _message("23", "member@example.com")

    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[message]),
        patch("thenetwork.worker.producer.observe_primary_intake_batch") as observe,
        patch("thenetwork.worker.producer.process_email"),
        patch("thenetwork.worker.producer.mark_messages_seen"),
    ):
        assert _poll_mailbox_and_enqueue("relay") == 1

    observe.assert_not_called()
