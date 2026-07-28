from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from limits import storage, strategies

from thenetwork.agent.deps import AgentCapabilities, AgentDeps
from thenetwork.settings import Settings


def _mock_sender_lookup(sender_id: str | None) -> MagicMock:
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = None
    mock_session.exec.return_value.first.return_value = sender_id
    return mock_session


def _consent_not_handled() -> SimpleNamespace:
    return SimpleNamespace(handled=False, sent_email_memories=[])


def _reset_dispatch_limiter() -> None:
    from thenetwork.agent import tools

    tools._dispatch_storage = storage.MemoryStorage()
    tools._dispatch_limiter = strategies.FixedWindowRateLimiter(tools._dispatch_storage)


def _ctx(
    *,
    sender_email: str = "new@example.com",
    sender_user_id: str | None = None,
    sender_authenticated: bool = True,
    inbound_subject: str = "Question",
    inbound_body: str = "Hi",
    capabilities: AgentCapabilities | None = None,
) -> SimpleNamespace:
    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
    )
    settings.dispatch_max_sends_per_run = 99
    settings.dispatch_recipient_daily_cap = 99
    settings.dispatch_sender_reply_daily_cap = 99
    deps = AgentDeps(
        settings=settings,
        capabilities=capabilities if capabilities is not None else AgentCapabilities(),
        sender_email=sender_email,
        sender_user_id=sender_user_id,
        sender_authenticated=sender_authenticated,
        inbound_subject=inbound_subject,
        inbound_body=inbound_body,
    )
    return SimpleNamespace(deps=deps, messages=())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "subject", "sender_id"),
    [
        ("Hi", "", None),
        ("", "What is this?", None),
        (" \n", "Hello", "known-person"),
    ],
    ids=("short-unknown", "subject-only-unknown", "blank-known"),
)
async def test_authenticated_short_messages_reach_agent(
    body: str, subject: str, sender_id: str | None
) -> None:
    from thenetwork.worker.tasks import process_email

    mock_session = _mock_sender_lookup(sender_id)
    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch(
            "thenetwork.worker.tasks.check_rate_limit", return_value=True
        ) as check_rate_limit,
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ) as scan_content,
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=_consent_not_handled(),
        ),
        patch(
            "thenetwork.worker.tasks.record_sent_email_memories",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email", new_callable=AsyncMock
        ) as run_agent,
    ):
        await process_email.func(
            sender_email="sender@example.com",
            subject=subject,
            body=body,
            sender_authenticated=True,
        )

    check_rate_limit.assert_called_once_with(
        "sender@example.com", sender_authenticated=True
    )
    scan_content.assert_awaited_once_with(body)
    send_reply.assert_not_called()
    run_agent.assert_awaited_once()
    assert run_agent.await_args.kwargs["email_subject"] == subject
    assert run_agent.await_args.kwargs["email_body"] == body
    assert run_agent.await_args.kwargs["sender_user_id"] == sender_id


@pytest.mark.asyncio
async def test_unauthenticated_unknown_short_message_stops_after_safety_gates() -> None:
    from thenetwork.worker.tasks import process_email

    mock_session = _mock_sender_lookup(None)
    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ) as scan_content,
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=_consent_not_handled(),
        ),
        patch(
            "thenetwork.worker.tasks.record_sent_email_memories",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email", new_callable=AsyncMock
        ) as run_agent,
    ):
        await process_email.func(
            sender_email="spoof@example.com",
            subject="Hello",
            body="Hi",
            sender_authenticated=False,
        )

    scan_content.assert_awaited_once_with("Hi")
    send_reply.assert_not_called()
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limited_short_first_contact_does_not_reach_agent() -> None:
    from thenetwork.worker.tasks import process_email

    mock_session = _mock_sender_lookup(None)
    with (
        patch("thenetwork.worker.tasks.get_session", return_value=mock_session),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=False),
        patch("thenetwork.worker.tasks.scan_content", new_callable=AsyncMock) as scan,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email", new_callable=AsyncMock
        ) as run_agent,
    ):
        await process_email.func(
            sender_email="new@example.com",
            subject="Hello",
            body="Hi",
            sender_authenticated=True,
        )

    scan.assert_not_awaited()
    send_reply.assert_not_called()
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_can_choose_fixed_welcome_without_registration_or_escalation() -> (
    None
):
    from thenetwork.agent.tools import send_first_contact_welcome
    from thenetwork.email.render import (
        FirstContactWelcomeEmailContext,
        FixedEmailTemplate,
    )

    _reset_dispatch_limiter()
    send_reply = MagicMock()
    notify_admins = MagicMock()
    get_session = MagicMock()
    ctx = _ctx(
        inbound_subject="",
        inbound_body="Hi",
        capabilities=AgentCapabilities(
            send_reply=send_reply,
            notify_admins=notify_admins,
            default_session_factory=get_session,
        ),
    )
    ctx.deps.inbound_message_id = "<abc123@example.com>"
    ctx.deps.inbound_references = "<root@example.com>"
    ctx.deps.inbound_body_for_quote = "Hi"
    ctx.deps.inbound_date = "Sat, 04 Jul 2026 12:00:00 -0700"

    result = await send_first_contact_welcome(ctx)

    assert result == {"status": "sent"}
    send_reply.assert_called_once_with(
        to_address="new@example.com",
        subject="How to join",
        fixed_template=FixedEmailTemplate.FIRST_CONTACT_WELCOME,
        fixed_context=FirstContactWelcomeEmailContext(),
        in_reply_to="<abc123@example.com>",
        references="<root@example.com> <abc123@example.com>",
        quoted_body_text="Hi",
        quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
    )
    get_session.assert_not_called()
    notify_admins.assert_not_called()
    assert ctx.deps.sender_user_id is None
    assert ctx.deps.server_side_send_count == 1


@pytest.mark.asyncio
async def test_fixed_welcome_is_limited_once_per_sender_per_day() -> None:
    from thenetwork.agent.tools import send_first_contact_welcome

    _reset_dispatch_limiter()
    send_reply = MagicMock()
    capabilities = AgentCapabilities(send_reply=send_reply)
    first_ctx = _ctx(sender_email="New@Example.COM", capabilities=capabilities)
    second_ctx = _ctx(sender_email="new@example.com", capabilities=capabilities)

    first = await send_first_contact_welcome(first_ctx)
    second = await send_first_contact_welcome(second_ctx)

    assert first == {"status": "sent"}
    assert second == {
        "status": "limited",
        "reason": "welcome_daily_cap",
        "limit": 1,
    }
    send_reply.assert_called_once()


@pytest.mark.asyncio
async def test_failed_welcome_delivery_preserves_quota_for_retry() -> None:
    from thenetwork.agent.tools import send_first_contact_welcome

    _reset_dispatch_limiter()
    send_reply = MagicMock(side_effect=[RuntimeError("SMTP unavailable"), None])
    capabilities = AgentCapabilities(send_reply=send_reply)
    first_ctx = _ctx(sender_email="New@Example.COM", capabilities=capabilities)
    retry_ctx = _ctx(sender_email="new@example.com", capabilities=capabilities)

    with pytest.raises(RuntimeError, match="SMTP unavailable"):
        await send_first_contact_welcome(first_ctx)
    retry = await send_first_contact_welcome(retry_ctx)

    assert retry == {"status": "sent"}
    assert send_reply.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (
            _ctx(
                sender_user_id="known-person",
                capabilities=AgentCapabilities(send_reply=MagicMock()),
            ),
            {"status": "error", "reason": "already_registered"},
        ),
        (
            _ctx(
                sender_authenticated=False,
                capabilities=AgentCapabilities(send_reply=MagicMock()),
            ),
            {"status": "error", "reason": "sender_not_authenticated"},
        ),
        (
            _ctx(
                inbound_body="Please do not retain my data. I am opting out.",
                capabilities=AgentCapabilities(send_reply=MagicMock()),
            ),
            {"status": "no_action", "reason": "sender_declined_participation"},
        ),
    ],
    ids=("known", "unauthenticated", "declined"),
)
async def test_fixed_welcome_rejects_ineligible_sender(
    ctx: SimpleNamespace, expected: dict[str, str]
) -> None:
    from thenetwork.agent.tools import send_first_contact_welcome

    _reset_dispatch_limiter()
    result = await send_first_contact_welcome(ctx)

    assert result == expected
    ctx.deps.capabilities.send_reply.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_sender_can_receive_only_one_response_per_run() -> None:
    from thenetwork.agent.tools import reply_to_sender, send_first_contact_welcome

    _reset_dispatch_limiter()
    send_reply = MagicMock()
    ctx = _ctx(capabilities=AgentCapabilities(send_reply=send_reply))
    welcome = await send_first_contact_welcome(ctx)
    # A later registration in the same run must not turn the sender into a
    # second available reply target after the fixed welcome was sent.
    ctx.deps.sender_user_id = "newly-registered"
    second = await reply_to_sender(
        ctx,
        subject="Re: Question",
        body_text="A second response.",
    )

    assert welcome == {"status": "sent"}
    assert second == {
        "status": "limited",
        "reason": "max_sends_per_run",
        "limit": 1,
    }
    send_reply.assert_called_once()
