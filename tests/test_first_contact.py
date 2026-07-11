from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from limits import storage, strategies


def _mock_sender_lookup(sender_id: str | None) -> MagicMock:
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = None
    mock_session.exec.return_value.first.return_value = sender_id
    return mock_session


def _reset_welcome_limiter() -> None:
    from thenetwork.worker import tasks

    tasks._welcome_storage = storage.MemoryStorage()
    tasks._welcome_limiter = strategies.FixedWindowRateLimiter(tasks._welcome_storage)


@pytest.mark.asyncio
async def test_near_empty_authenticated_unknown_sender_gets_welcome_after_rate_limit():
    from thenetwork.email.outbound import FIRST_CONTACT_WELCOME_REPLY
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch(
            "thenetwork.worker.tasks.check_rate_limit", return_value=True
        ) as check_rate_limit,
        patch("thenetwork.worker.tasks.scan_content") as scan_content,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="new@example.com",
            subject="",
            body="Hi",
            sender_authenticated=True,
        )

    check_rate_limit.assert_called_once_with(
        "new@example.com",
        sender_authenticated=True,
    )
    scan_content.assert_not_called()
    send_reply.assert_called_once_with(
        to_address="new@example.com",
        subject="How to join",
        body_text=FIRST_CONTACT_WELCOME_REPLY,
        include_footer=False,
    )
    mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_first_contact_welcome_threads_reply_when_message_id_present():
    from thenetwork.email.outbound import FIRST_CONTACT_WELCOME_REPLY
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()),
    ):
        await process_email.func(
            sender_email="new@example.com",
            subject="",
            body="Hi",
            sender_authenticated=True,
            inbound_message_id="<abc123@example.com>",
            inbound_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    send_reply.assert_called_once_with(
        to_address="new@example.com",
        subject="How to join",
        body_text=FIRST_CONTACT_WELCOME_REPLY,
        include_footer=False,
        in_reply_to="<abc123@example.com>",
        references="<abc123@example.com>",
        quoted_body_text="Hi",
        quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
    )


@pytest.mark.asyncio
async def test_first_contact_welcome_appends_to_references_chain():
    from thenetwork.email.outbound import FIRST_CONTACT_WELCOME_REPLY
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()),
    ):
        await process_email.func(
            sender_email="new@example.com",
            subject="",
            body="Hi",
            sender_authenticated=True,
            inbound_message_id="<abc123@example.com>",
            inbound_references="<root@example.com> <parent@example.com>",
            inbound_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    send_reply.assert_called_once_with(
        to_address="new@example.com",
        subject="How to join",
        body_text=FIRST_CONTACT_WELCOME_REPLY,
        include_footer=False,
        in_reply_to="<abc123@example.com>",
        references="<root@example.com> <parent@example.com> <abc123@example.com>",
        quoted_body_text="Hi",
        quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
    )


@pytest.mark.asyncio
async def test_first_contact_welcome_drops_unsafe_message_id():
    from thenetwork.email.outbound import FIRST_CONTACT_WELCOME_REPLY
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()),
    ):
        await process_email.func(
            sender_email="new@example.com",
            subject="",
            body="Hi",
            sender_authenticated=True,
            inbound_message_id="<abc123@example.com>\r\nBcc: attacker@example.com",
            inbound_references="<root@example.com>",
            inbound_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    send_reply.assert_called_once_with(
        to_address="new@example.com",
        subject="How to join",
        body_text=FIRST_CONTACT_WELCOME_REPLY,
        include_footer=False,
    )


@pytest.mark.asyncio
async def test_near_empty_known_authenticated_sender_stays_silent():
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup("person-id")

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="known@example.com",
            subject="Hello",
            body=" ",
            sender_authenticated=True,
        )

    send_reply.assert_not_called()
    mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limited_blank_known_sender_stays_silent():
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=False),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="known@example.com",
            subject="Hello",
            body=" ",
            sender_authenticated=True,
        )

    send_reply.assert_not_called()
    mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_near_empty_unauthenticated_unknown_sender_stays_silent():
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="spoof@example.com",
            subject="Hello",
            body="Hi",
            sender_authenticated=False,
        )

    send_reply.assert_not_called()
    mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_welcome_is_limited_per_sender():
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()),
    ):
        for _ in range(2):
            await process_email.func(
                sender_email="new@example.com",
                subject="Hello",
                body="Hi",
                sender_authenticated=True,
            )

    send_reply.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limited_blank_first_contact_does_not_get_welcome():
    from thenetwork.worker.tasks import process_email

    _reset_welcome_limiter()
    mock_session = _mock_sender_lookup(None)

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=False),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        await process_email.func(
            sender_email="new@example.com",
            subject="Hello",
            body="Hi",
            sender_authenticated=True,
        )

    send_reply.assert_not_called()
    mock_agent.assert_not_called()
