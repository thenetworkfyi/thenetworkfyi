"""Tests for the admin channel: auth, command parsing, and task routing."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


# ─── Auth ────────────────────────────────────────────────────────────────────

def _settings(emails=("admin@example.com",), token="s3cr3t"):
    s = MagicMock()
    s.admin_emails = list(emails)
    s.admin_token = token
    return s


def test_is_admin_request_valid():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: s3cr3t\nDo the thing."
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert is_admin_request("admin@example.com", "ADMIN: status", body)


def test_is_admin_request_case_insensitive_subject():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: s3cr3t"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert is_admin_request("admin@example.com", "admin: status", body)


def test_is_admin_request_wrong_sender():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: s3cr3t"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("attacker@evil.com", "ADMIN: status", body)


def test_is_admin_request_wrong_token():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: wrongtoken"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", "ADMIN: status", body)


def test_is_admin_request_missing_token_line():
    from thenetwork.admin.auth import is_admin_request
    body = "Just some text without token."
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", "ADMIN: status", body)


def test_is_admin_request_not_admin_subject():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: s3cr3t"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", "Hello there", body)


def test_is_admin_request_disabled_when_no_token():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: s3cr3t"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings(token="")):
        assert not is_admin_request("admin@example.com", "ADMIN: status", body)


def test_is_admin_request_disabled_when_no_emails():
    from thenetwork.admin.auth import is_admin_request
    body = "TOKEN: s3cr3t"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings(emails=[])):
        assert not is_admin_request("admin@example.com", "ADMIN: status", body)


def test_extract_command():
    from thenetwork.admin.auth import extract_command
    assert extract_command("ADMIN: status") == "status"
    assert extract_command("ADMIN: search rust engineers") == "search rust engineers"
    assert extract_command("admin: forget abc-123") == "forget abc-123"


def test_extract_body_text_strips_token_and_quotes():
    from thenetwork.admin.auth import extract_body_text
    body = "TOKEN: s3cr3t\nReal content here.\n> Quoted line\nMore content."
    result = extract_body_text(body)
    assert "TOKEN" not in result
    assert "Quoted line" not in result
    assert "Real content here." in result
    assert "More content." in result


# ─── Task routing ────────────────────────────────────────────────────────────

def test_process_email_routes_admin_to_handler():
    """Admin emails are handled by admin channel, not the agent."""
    import asyncio

    from thenetwork.worker.tasks import process_email

    mock_reply = AsyncMock(return_value="People:   3\nMemories: 10\n")
    mock_send = MagicMock()

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.is_admin_request", return_value=True), \
         patch("thenetwork.worker.tasks.extract_command", return_value="status"), \
         patch("thenetwork.worker.tasks.extract_body_text", return_value=""), \
         patch("thenetwork.worker.tasks.handle_admin_command", mock_reply), \
         patch("thenetwork.worker.tasks.send_reply", mock_send), \
         patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent:
        asyncio.run(process_email.func(
            sender_email="admin@example.com",
            subject="ADMIN: status",
            body="TOKEN: s3cr3t",
        ))

    mock_reply.assert_called_once_with("status", "")
    mock_send.assert_called_once()
    mock_agent.assert_not_called()


def test_process_email_non_admin_goes_to_agent():
    """Normal emails are NOT intercepted by the admin channel."""
    import asyncio

    from thenetwork.worker.tasks import process_email

    mock_agent = AsyncMock()

    with patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.is_admin_request", return_value=False), \
         patch("thenetwork.worker.tasks.get_session") as mock_sess, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        sess_cm = MagicMock()
        sess_cm.__enter__ = MagicMock(return_value=MagicMock(
            exec=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        ))
        sess_cm.__exit__ = MagicMock(return_value=False)
        mock_sess.return_value = sess_cm
        asyncio.run(process_email.func(
            sender_email="user@example.com",
            subject="Hello!",
            body="I'm looking for a cofounder.",
        ))

    mock_agent.assert_called_once()


# ─── Command dispatch ─────────────────────────────────────────────────────────

def test_handle_admin_command_unknown():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("explode", ""))
    assert "Unknown command" in result
    assert "explode" in result


def test_handle_admin_command_search_no_query():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("search", ""))
    assert "Usage" in result


def test_handle_admin_command_show_no_arg():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("show", ""))
    assert "Usage" in result


def test_handle_admin_command_forget_no_arg():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("forget", ""))
    assert "Usage" in result


def test_handle_admin_command_remember_no_body():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    result = asyncio.run(handle_admin_command("remember", "   "))
    assert "No memory text" in result
