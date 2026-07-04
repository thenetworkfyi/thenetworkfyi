"""Unit tests for the post-send IMAP Sent-folder append (thenetwork/email/outbound.py)."""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

from imap_tools import MailMessageFlags

from thenetwork.audit import LOGGER_NAME


def _events(caplog) -> list[dict]:
    return [json.loads(record.message) for record in caplog.records if record.name == LOGGER_NAME]


def _mock_settings(**overrides):
    s = MagicMock()
    s.smtp_host = "smtp.example.com"
    s.smtp_port = 587
    s.imap_account = "agent@example.com"
    s.imap_password = "secret"
    s.smtp_account = "agent@example.com"
    s.smtp_password = "secret"
    s.imap_host = "imap.example.com"
    s.imap_port = 993
    s.imap_sent_folder = "Sent"
    s.growth_footer_enabled = False
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _mock_smtp():
    smtp_instance = MagicMock()
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)
    return smtp_instance


def _mock_mailbox_success():
    mb_instance = MagicMock()
    mb_instance.__enter__ = MagicMock(return_value=mb_instance)
    mb_instance.__exit__ = MagicMock(return_value=False)
    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.return_value = mb_instance
    return mock_mailbox, mb_instance


def test_append_called_on_success():
    """After a successful SMTP send, the composed message is appended to IMAP."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    mb_instance.append.assert_called_once()


def test_append_uses_configured_folder_name():
    """The folder passed to append() comes from settings.imap_sent_folder."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings(imap_sent_folder="MyCustomSent")), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    args, kwargs = mb_instance.append.call_args
    folder = args[1] if len(args) > 1 else kwargs.get("folder")
    assert folder == "MyCustomSent"


def test_append_sets_seen_flag():
    """The appended message must be flagged \\Seen so it doesn't show as unread."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    _, kwargs = mb_instance.append.call_args
    assert kwargs.get("flag_set") == [MailMessageFlags.SEEN]


def test_append_receives_exact_composed_message():
    """The bytes appended must be the same message that was SMTP-sent."""
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=smtp_instance), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    args, _ = mb_instance.append.call_args
    appended_bytes = args[0]
    assert appended_bytes == captured[0].as_bytes()


def test_append_failure_does_not_propagate():
    """An IMAP append failure must not raise — the SMTP send already succeeded."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.side_effect = OSError("connection refused")

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        # Must not raise.
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)


def test_append_failure_is_audit_logged_as_error(caplog):
    """A failed append is audit-logged with outcome=error, no folder/address/content."""
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.side_effect = OSError("connection refused")

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    events = _events(caplog)
    append_events = [e for e in events if e.get("event") == "email.imap_append.completed"]
    assert len(append_events) == 1
    assert append_events[0]["outcome"] == "error"
    assert append_events[0]["error_type"] == "OSError"

    serialized = "\n".join(record.message for record in caplog.records)
    assert "MyCustomSent" not in serialized
    assert "Sent" not in serialized
    assert "bob@example.com" not in serialized
    assert "Hello" not in serialized


def test_append_success_is_audit_logged(caplog):
    """A successful append is audit-logged with outcome=success."""
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    events = _events(caplog)
    append_events = [e for e in events if e.get("event") == "email.imap_append.completed"]
    assert len(append_events) == 1
    assert append_events[0]["outcome"] == "success"

    serialized = "\n".join(record.message for record in caplog.records)
    assert "bob@example.com" not in serialized
    assert "Hello" not in serialized
