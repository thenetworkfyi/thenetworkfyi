"""Worker dispatch tests for participant-to-participant proxy replies."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.audit import LOGGER_NAME


TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DOMAIN = "relay.example.com"
PROXY = f"hidden-{TOKEN}@{DOMAIN}"


def _empty_session():
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.get.return_value = None
    return session


def _events(caplog):
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


@pytest.mark.parametrize(
    ("sender", "destination"),
    [
        ("alice@example.com", "bob@example.com"),
        ("bob@example.com", "alice@example.com"),
    ],
)
@pytest.mark.asyncio
async def test_process_email_relays_both_directions_before_agent_paths(
    sender, destination, caplog
):
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with (
        patch(
            "thenetwork.worker.tasks.get_settings",
            return_value=SimpleNamespace(relay_domain=DOMAIN),
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.resolve_relay_destination",
            return_value=destination,
        ) as resolve,
        patch("thenetwork.worker.tasks.send_relay_email") as send,
        patch("thenetwork.worker.tasks.scan_content") as scan,
        patch("thenetwork.worker.tasks.process_consent_reply") as consent,
        patch(
            "thenetwork.worker.tasks.record_sent_email_memories", AsyncMock()
        ) as memories,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as agent,
    ):
        await process_email.func(
            sender_email=sender,
            sender_authenticated=True,
            recipient_address=PROXY,
            subject="Re: Project details",
            body="Unchanged reply body\nsecond line",
            trace_id="relay-trace",
        )

    resolve.assert_called_once()
    send.assert_called_once_with(
        to_address=destination,
        proxy_address=PROXY,
        subject="Re: Project details",
        body_text="Unchanged reply body\nsecond line",
        source_message=None,
        trace_id="relay-trace",
    )
    scan.assert_not_called()
    consent.assert_not_called()
    memories.assert_not_called()
    agent.assert_not_called()
    serialized = json.dumps(_events(caplog))
    assert sender not in serialized
    assert destination not in serialized
    assert PROXY not in serialized
    assert "Unchanged reply body" not in serialized


@pytest.mark.parametrize(
    ("recipient", "destination"),
    [
        (PROXY, None),
        (f"hidden-not-a-token@{DOMAIN}", "bob@example.com"),
        (f"hidden-not-a-token@{DOMAIN}.", "bob@example.com"),
    ],
)
@pytest.mark.asyncio
async def test_process_email_rejects_unroutable_relay_attempts(recipient, destination):
    from thenetwork.worker.tasks import process_email

    with (
        patch(
            "thenetwork.worker.tasks.get_settings",
            return_value=SimpleNamespace(relay_domain=DOMAIN),
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.resolve_relay_destination",
            return_value=destination,
        ) as resolve,
        patch("thenetwork.worker.tasks.send_relay_email") as send,
        patch("thenetwork.worker.tasks.scan_content") as scan,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as agent,
    ):
        await process_email.func(
            sender_email="mallory@example.com",
            sender_authenticated=True,
            recipient_address=recipient,
            subject="Relay attempt",
            body="Do not deliver this",
        )

    send.assert_not_called()
    scan.assert_not_called()
    agent.assert_not_called()
    if "not-a-token" in recipient:
        resolve.assert_not_called()
    else:
        resolve.assert_called_once()


@pytest.mark.asyncio
async def test_process_email_rate_limits_relay_before_resolution():
    from thenetwork.worker.tasks import process_email

    with (
        patch(
            "thenetwork.worker.tasks.get_settings",
            return_value=SimpleNamespace(relay_domain=DOMAIN),
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=False),
        patch("thenetwork.worker.tasks.resolve_relay_destination") as resolve,
        patch("thenetwork.worker.tasks.send_relay_email") as send,
    ):
        await process_email.func(
            sender_email="alice@example.com",
            sender_authenticated=True,
            recipient_address=PROXY,
            subject="Relay reply",
            body="Rate limited body",
        )

    resolve.assert_not_called()
    send.assert_not_called()


@pytest.mark.asyncio
async def test_process_email_relay_send_failure_propagates_for_task_retry():
    from thenetwork.worker.tasks import process_email

    with (
        patch(
            "thenetwork.worker.tasks.get_settings",
            return_value=SimpleNamespace(relay_domain=DOMAIN),
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.resolve_relay_destination",
            return_value="bob@example.com",
        ),
        patch(
            "thenetwork.worker.tasks.send_relay_email",
            side_effect=RuntimeError("smtp unavailable"),
        ),
    ):
        with pytest.raises(RuntimeError, match="smtp unavailable"):
            await process_email.func(
                sender_email="alice@example.com",
                sender_authenticated=True,
                recipient_address=PROXY,
                subject="Relay reply",
                body="Retry this body",
            )


@pytest.mark.asyncio
async def test_nonrelay_recipient_keeps_existing_agent_path():
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.worker.tasks import process_email

    session = _empty_session()
    session.exec.return_value.first.return_value = "alice-id"
    agent = AsyncMock()
    with (
        patch(
            "thenetwork.worker.tasks.get_settings",
            return_value=SimpleNamespace(relay_domain=DOMAIN),
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ),
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
            sender_email="alice@example.com",
            sender_authenticated=True,
            recipient_address="join@example.com",
            subject="Ordinary message",
            body="Please help with this project",
        )

    relay_send.assert_not_called()
    agent.assert_awaited_once()
    assert agent.await_args.kwargs["email_subject"] == "Ordinary message"
    assert agent.await_args.kwargs["email_body"] == "Please help with this project"
