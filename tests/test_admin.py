"""Tests for the admin channel: auth, command parsing, and task routing."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch


# ─── Auth ────────────────────────────────────────────────────────────────────

def _settings(emails=("admin@example.com",), token="s3cr3t", window=300):
    s = MagicMock()
    s.admin_emails = list(emails)
    s.admin_token = token
    s.admin_replay_window_seconds = window
    return s


def _signed(token: str, subject: str) -> str:
    from thenetwork.admin.auth import sign_admin_request
    return sign_admin_request(token, subject)


def _fresh_nonce_session():
    """A get_session() mock whose nonce store is always empty (no replay seen)."""
    session = MagicMock()
    session.get.return_value = None
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, session


def test_is_admin_request_valid():
    from thenetwork.admin.auth import is_admin_request
    subject = "ADMIN: status"
    body = _signed("s3cr3t", subject) + "\nDo the thing."
    cm, session = _fresh_nonce_session()
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()), \
         patch("thenetwork.admin.auth.get_session", return_value=cm):
        assert is_admin_request("admin@example.com", subject, body)
    session.add.assert_called_once()


def test_is_admin_request_case_insensitive_subject():
    from thenetwork.admin.auth import is_admin_request
    subject = "admin: status"
    body = _signed("s3cr3t", subject)
    cm, _ = _fresh_nonce_session()
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()), \
         patch("thenetwork.admin.auth.get_session", return_value=cm):
        assert is_admin_request("admin@example.com", subject, body)


def test_is_admin_request_wrong_sender():
    from thenetwork.admin.auth import is_admin_request
    subject = "ADMIN: status"
    body = _signed("s3cr3t", subject)
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("attacker@evil.com", subject, body)


def test_is_admin_request_wrong_token():
    from thenetwork.admin.auth import is_admin_request
    subject = "ADMIN: status"
    body = _signed("wrongtoken", subject)
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", subject, body)


def test_is_admin_request_missing_signature_lines():
    from thenetwork.admin.auth import is_admin_request
    body = "Just some text without a signature."
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", "ADMIN: status", body)


def test_is_admin_request_not_admin_subject():
    from thenetwork.admin.auth import is_admin_request
    body = _signed("s3cr3t", "Hello there")
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", "Hello there", body)


def test_is_admin_request_disabled_when_no_token():
    from thenetwork.admin.auth import is_admin_request
    subject = "ADMIN: status"
    body = _signed("s3cr3t", subject)
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings(token="")):
        assert not is_admin_request("admin@example.com", subject, body)


def test_is_admin_request_disabled_when_no_emails():
    from thenetwork.admin.auth import is_admin_request
    subject = "ADMIN: status"
    body = _signed("s3cr3t", subject)
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings(emails=[])):
        assert not is_admin_request("admin@example.com", subject, body)


def test_is_admin_request_expired_timestamp():
    from thenetwork.admin.auth import _expected_signature, is_admin_request
    subject = "ADMIN: status"
    ts = str(int(time.time()) - 600)
    nonce = "a" * 32
    sig = _expected_signature("s3cr3t", subject, ts, nonce)
    body = f"TS: {ts}\nNONCE: {nonce}\nSIG: {sig}"
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings(window=300)):
        assert not is_admin_request("admin@example.com", subject, body)


def test_is_admin_request_rejects_replayed_nonce():
    from thenetwork.admin.auth import is_admin_request
    subject = "ADMIN: status"
    body = _signed("s3cr3t", subject)
    session = MagicMock()
    session.get.return_value = object()  # nonce already seen
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()), \
         patch("thenetwork.admin.auth.get_session", return_value=cm):
        assert not is_admin_request("admin@example.com", subject, body)
    session.add.assert_not_called()


def test_is_admin_request_signature_bound_to_subject():
    """A signature made for one subject must not authorize a different one."""
    from thenetwork.admin.auth import is_admin_request
    body = _signed("s3cr3t", "ADMIN: status")
    with patch("thenetwork.admin.auth.get_settings", return_value=_settings()):
        assert not is_admin_request("admin@example.com", "ADMIN: forget xyz", body)


def test_extract_command():
    from thenetwork.admin.auth import extract_command
    assert extract_command("ADMIN: status") == "status"
    assert extract_command("ADMIN: search rust engineers") == "search rust engineers"
    assert extract_command("admin: forget abc-123") == "forget abc-123"


def test_extract_body_text_strips_signature_and_quotes():
    from thenetwork.admin.auth import extract_body_text
    body = "TS: 123\nNONCE: abc\nSIG: def\nReal content here.\n> Quoted line\nMore content."
    result = extract_body_text(body)
    assert "TS:" not in result
    assert "NONCE:" not in result
    assert "SIG:" not in result
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
            body="TS: 1\nNONCE: abc\nSIG: def",
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
         patch("thenetwork.worker.tasks.get_session") as mock_gs, \
         patch("thenetwork.worker.tasks.run_agent_for_email", mock_agent):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.first.return_value = None
        mock_gs.return_value = mock_session
        asyncio.run(process_email.func(
            sender_email="user@example.com",
            subject="Hello",
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


def test_handle_admin_command_remember_refs_awaits_high_fidelity_sanitizer():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command
    from thenetwork.db.models import Person

    person = MagicMock(spec=Person)
    person.id = "user-alice"

    resolve_session = MagicMock()
    resolve_session.exec.return_value.first.return_value = person
    resolve_cm = MagicMock()
    resolve_cm.__enter__ = MagicMock(return_value=resolve_session)
    resolve_cm.__exit__ = MagicMock(return_value=False)

    write_session = MagicMock()
    added: list[object] = []
    write_session.add.side_effect = added.append
    write_cm = MagicMock()
    write_cm.__enter__ = MagicMock(return_value=write_session)
    write_cm.__exit__ = MagicMock(return_value=False)

    async def fake_sanitize(memory, session):
        memory.gist = "[name] knows privacy-preserving ML."
        return memory.gist

    with patch("thenetwork.admin.commands.embed_text", new=AsyncMock(return_value=[0.0] * 1536)) as mock_embed, \
         patch("thenetwork.admin.commands.get_session", side_effect=[resolve_cm, write_cm]), \
         patch(
             "thenetwork.admin.commands.sanitize_memory_high_fidelity",
             new=AsyncMock(side_effect=fake_sanitize),
         ) as mock_sanitize:
        result = asyncio.run(
            handle_admin_command(
                "remember refs:alice@example.com",
                "Alice Smith knows Bob through privacy-preserving ML.",
            )
        )

    assert "Stored memory" in result
    mock_sanitize.assert_awaited_once()
    mock_embed.assert_awaited_once_with("[name] knows privacy-preserving ML.")
    assert added[0].refs == ["user-alice"]
    assert "Alice" not in added[0].gist
    assert "Bob" not in added[0].gist


def test_handle_admin_command_remember_without_refs_does_not_sanitize():
    import asyncio
    from thenetwork.admin.commands import handle_admin_command

    write_session = MagicMock()
    added: list[object] = []
    write_session.add.side_effect = added.append
    write_cm = MagicMock()
    write_cm.__enter__ = MagicMock(return_value=write_session)
    write_cm.__exit__ = MagicMock(return_value=False)

    raw = "General note with no refs."

    with patch("thenetwork.admin.commands.embed_text", new=AsyncMock(return_value=[0.0] * 1536)) as mock_embed, \
         patch("thenetwork.admin.commands.get_session", return_value=write_cm), \
         patch("thenetwork.admin.commands.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize:
        result = asyncio.run(handle_admin_command("remember", raw))

    assert "Stored memory" in result
    mock_sanitize.assert_not_awaited()
    mock_embed.assert_awaited_once_with(raw)
    assert added[0].refs == []
    assert added[0].gist is None
