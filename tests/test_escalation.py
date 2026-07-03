"""Unit tests for the escalate tool."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from thenetwork.agent.deps import AgentDeps
from thenetwork.settings import Settings


def _make_settings(admin_emails=None):
    s = MagicMock(spec=Settings)
    s.admin_emails = admin_emails or []
    s.admin_token = "secret"
    return s


def _ctx(sender_email="user@test.com", sender_user_id=None, admin_emails=None):
    deps = AgentDeps(
        settings=_make_settings(admin_emails=admin_emails),
        sender_email=sender_email,
        sender_user_id=sender_user_id,
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
    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock), \
         patch("thenetwork.agent.tools.send_reply"):
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

    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock), \
         patch("thenetwork.agent.tools.send_reply"):
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

    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize, \
         patch("thenetwork.agent.tools.send_reply"):
        await escalate(_ctx(sender_user_id="user-abc"), reason="Unclear")

    mem = added_objects[0]
    assert mem.refs == ["user-abc"]
    mock_sanitize.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalate_empty_refs_for_unknown_sender():
    from thenetwork.agent.tools import escalate

    cm, session = _mock_session()
    added_objects = []
    session.add.side_effect = added_objects.append

    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize, \
         patch("thenetwork.agent.tools.send_reply"):
        await escalate(_ctx(sender_user_id=None), reason="New sender, unclear intent")

    mem = added_objects[0]
    assert mem.refs == []
    mock_sanitize.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalate_notifies_all_admin_emails():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock), \
         patch("thenetwork.agent.tools.send_reply") as mock_send:
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
    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock), \
         patch("thenetwork.agent.tools.send_reply") as mock_send:
        await escalate(_ctx(admin_emails=[]), reason="Unclear")

    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_escalate_notification_includes_sender_and_reason():
    from thenetwork.agent.tools import escalate

    cm, _ = _mock_session()
    with patch("thenetwork.agent.tools.embed_text", new=AsyncMock(return_value=[0.0] * 1536)), \
         patch("thenetwork.agent.tools.get_session", return_value=cm), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock), \
         patch("thenetwork.agent.tools.send_reply") as mock_send:
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
