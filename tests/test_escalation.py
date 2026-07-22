"""Unit tests for the escalate tool."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from thenetwork.agent.deps import AgentDeps
from thenetwork.settings import Settings


def _make_settings(admin_emails=None):
    s = MagicMock(spec=Settings)
    s.admin_emails = admin_emails or []
    return s


def _ctx(
    sender_email="user@test.com",
    sender_user_id=None,
    admin_emails=None,
    sender_authenticated=False,
    inbound_subject="",
):
    deps = AgentDeps(
        settings=_make_settings(admin_emails=admin_emails),
        sender_email=sender_email,
        sender_user_id=sender_user_id,
        sender_authenticated=sender_authenticated,
        inbound_subject=inbound_subject,
        session_factory=None,
    )
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _mock_session():
    session = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, session


@pytest.mark.asyncio
async def test_escalate_returns_escalated_status():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.agent.tools.notify_admins"),
    ):
        result = await escalate(_ctx(), reason="Intent unclear")

    assert result["status"] == "escalated"
    assert "memory_id" in result


@pytest.mark.asyncio
async def test_escalate_stores_memory_with_escalation_marker():
    from thenetwork.agent.tools import escalate
    from thenetwork.db.models import Memory

    cm, session = _mock_session()
    added_objects = []
    session.add.side_effect = added_objects.append

    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.agent.tools.notify_admins"),
    ):
        await escalate(_ctx(), reason="Cannot determine intent")

    assert len(added_objects) == 1
    mem = added_objects[0]
    assert isinstance(mem, Memory)
    assert "[ESCALATED]" in mem.text
    assert "Cannot determine intent" in mem.text


@pytest.mark.asyncio
async def test_escalate_includes_sender_id_in_refs_when_known():
    from thenetwork.agent.tools import escalate

    cm, session = _mock_session()
    added_objects = []
    session.add.side_effect = added_objects.append
    sanitized = "[name] asked for human review."

    async def fake_sanitize(memory, session):
        memory.gist = sanitized
        return memory.gist

    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ) as mock_embed,
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new=AsyncMock(side_effect=fake_sanitize),
        ) as mock_sanitize,
        patch("thenetwork.agent.tools.notify_admins"),
    ):
        await escalate(_ctx(sender_user_id="user-abc"), reason="Unclear")

    mem = added_objects[0]
    assert mem.refs == ["user-abc"]
    mock_sanitize.assert_awaited_once()
    mock_embed.assert_awaited_once_with(sanitized)


@pytest.mark.asyncio
async def test_escalate_empty_refs_for_unknown_sender():
    from thenetwork.agent.tools import escalate

    cm, session = _mock_session()
    added_objects = []
    session.add.side_effect = added_objects.append
    reason = "New sender, unclear intent"
    raw = f"[ESCALATED] {reason}"

    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ) as mock_embed,
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ) as mock_sanitize,
        patch("thenetwork.agent.tools.notify_admins"),
    ):
        await escalate(_ctx(sender_user_id=None), reason=reason)

    mem = added_objects[0]
    assert mem.refs == []
    mock_sanitize.assert_not_awaited()
    mock_embed.assert_awaited_once_with(raw)


@pytest.mark.asyncio
async def test_escalate_notifies_all_admin_emails():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.email.outbound.send_reply") as mock_send,
    ):
        await escalate(
            _ctx(
                sender_email="user@test.com",
                admin_emails=["admin1@example.com", "admin2@example.com"],
            ),
            reason="Ambiguous request",
        )

    assert mock_send.call_count == 2
    called_to = {c.kwargs["to_address"] for c in mock_send.call_args_list}
    assert called_to == {"admin1@example.com", "admin2@example.com"}


@pytest.mark.asyncio
async def test_escalate_no_notification_when_no_admin_emails():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.email.outbound.send_reply") as mock_send,
    ):
        await escalate(_ctx(admin_emails=[]), reason="Unclear")

    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_escalate_notification_includes_sender_and_reason():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.email.outbound.send_reply") as mock_send,
    ):
        await escalate(
            _ctx(
                sender_email="user@example.com",
                admin_emails=["admin@example.com"],
            ),
            reason="Sensitive topic, needs human judgment",
        )

    body = mock_send.call_args.kwargs["body_text"]
    assert "user@example.com" in body
    assert "Sensitive topic, needs human judgment" in body


@pytest.mark.asyncio
async def test_escalate_welcomes_and_notifies_admins_for_authenticated_unknown_sender():
    from thenetwork.agent.tools import escalate
    from thenetwork.email.render import (
        FirstContactWelcomeEmailContext,
        FixedEmailTemplate,
    )

    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ) as mock_embed,
        patch("thenetwork.agent.tools.get_session") as mock_get_session,
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ) as mock_sanitize,
        patch("thenetwork.agent.tools.audit_span_completion") as mock_completion,
        patch("thenetwork.agent.tools.notify_admins") as mock_notify,
        patch("thenetwork.agent.tools.send_reply") as mock_send,
    ):
        ctx = _ctx(
            sender_email="new@example.com",
            sender_authenticated=True,
            inbound_subject="Question",
            admin_emails=["admin@example.com"],
        )
        ctx.deps.trace_id = "trace-test-123"
        result = await escalate(ctx, reason="Ambiguous first contact")

    assert result == {"status": "welcomed_and_escalated"}
    assert ctx.deps.terminal_action_taken is True
    mock_completion.assert_called_once_with(tool_outcome="welcomed_and_escalated")
    mock_send.assert_called_once_with(
        to_address="new@example.com",
        subject="Re: Question",
        fixed_template=FixedEmailTemplate.FIRST_CONTACT_WELCOME,
        fixed_context=FirstContactWelcomeEmailContext(),
        trace_id="trace-test-123",
    )
    mock_get_session.assert_not_called()
    mock_embed.assert_not_awaited()
    mock_sanitize.assert_not_awaited()
    mock_notify.assert_called_once_with(
        ctx.deps.settings,
        "[The Network] Manual reply needed: new@example.com",
        "Email from new@example.com was escalated for human review.\n\n"
        "Reason: Ambiguous first contact\n\n"
        "Trace ID: trace-test-123\n\n"
        "Please reply to new@example.com manually.",
        trace_id="trace-test-123",
    )


@pytest.mark.asyncio
async def test_explicit_unknown_sender_opt_out_is_not_welcomed_or_escalated():
    from thenetwork.agent.tools import escalate, register_person

    ctx = _ctx(
        sender_email="private@example.com",
        sender_authenticated=True,
        inbound_subject="Privacy request",
        admin_emails=["admin@example.com"],
    )
    ctx.deps.inbound_body = (
        "Please do not retain information about me. I am opting out and do not "
        "want to participate."
    )

    with (
        patch("thenetwork.agent.tools.notify_admins") as mock_notify,
        patch("thenetwork.agent.tools.send_reply") as mock_send,
        patch("thenetwork.agent.tools.get_session") as mock_session,
    ):
        registration = await register_person(ctx, name="Private Sender")
        escalation = await escalate(ctx, reason="Unclear first contact")

    assert registration == {
        "status": "error",
        "reason": "sender_declined_participation",
    }
    assert escalation == {
        "status": "no_action",
        "reason": "sender_declined_participation",
    }
    assert ctx.deps.terminal_action_taken is False
    mock_send.assert_not_called()
    mock_notify.assert_not_called()
    mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_escalate_does_not_acknowledge_unauthenticated_sender():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch("thenetwork.agent.tools.get_session", return_value=cm),
        patch(
            "thenetwork.agent.tools.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.agent.tools.notify_admins"),
        patch("thenetwork.agent.tools.send_reply") as mock_send,
    ):
        await escalate(
            _ctx(
                sender_email="spoof@example.com",
                sender_authenticated=False,
            ),
            reason="Ambiguous first contact",
        )

    mock_send.assert_not_called()
