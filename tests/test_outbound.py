"""Unit tests for the post-send IMAP Sent-folder append (thenetwork/email/outbound.py)."""
from __future__ import annotations

import json
import logging
from email.utils import parsedate_to_datetime
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
    s.email_from = "agent@example.com"
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


def test_send_reply_sets_date_and_message_id_headers():
    """The built message must carry parseable Date/Message-ID headers so the
    SMTP-sent copy and the IMAP Sent append (verbatim bytes of the same
    message) both show a date and can thread, instead of relying on the
    submission MTA to stamp Date."""
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=smtp_instance), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello", include_footer=False)

    sent_msg = captured[0]
    assert sent_msg["Date"] is not None
    # Must be parseable per RFC 2822/5322, not just a non-empty string.
    parsedate_to_datetime(sent_msg["Date"])

    assert sent_msg["Message-ID"] is not None
    message_id = sent_msg["Message-ID"]
    assert message_id.startswith("<") and message_id.endswith(">")

    # The IMAP Sent append must carry the identical headers, since it is the
    # verbatim bytes of the same message object.
    args, _ = mb_instance.append.call_args
    appended_bytes = args[0]
    assert f"Date: {sent_msg['Date']}".encode() in appended_bytes
    assert f"Message-ID: {message_id}".encode() in appended_bytes


def test_send_reply_appends_plain_text_quoted_trail():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=smtp_instance), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            body_html="<p>Hello</p>",
            quoted_body_text="Original line\n> old quote\nSecond line",
            quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
            include_footer=False,
        )

    msg = captured[0]
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "On Sat, 04 Jul 2026 12:00:00 -0700, you wrote:" in plain
    assert "> Original line" in plain
    assert "> Second line" in plain
    assert "> old quote" not in plain
    assert "On Sat, 04 Jul 2026 12:00:00 -0700, you wrote:" in html
    assert "<blockquote>Original line" in html
    assert "Second line</blockquote>" in html
    assert "old quote" not in html


def test_send_reply_escapes_html_quoted_trail():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=smtp_instance), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            body_html="<p>Hello</p>",
            quoted_body_text="<script>steal()</script>",
            include_footer=False,
        )

    html = captured[0].get_body(preferencelist=("html",)).get_content()
    assert "<script>steal()</script>" not in html
    assert "&lt;script&gt;steal()&lt;/script&gt;" in html


def test_send_reply_plain_text_only_quote_stays_singlepart():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=smtp_instance), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line",
            include_footer=False,
        )

    msg = captured[0]
    assert not msg.is_multipart()
    assert "On an earlier message, you wrote:" in msg.get_content()


def test_send_reply_places_growth_footer_before_quoted_trail():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch(
        "thenetwork.email.outbound.get_settings",
        return_value=_mock_settings(growth_footer_enabled=True),
    ), \
         patch("smtplib.SMTP", return_value=smtp_instance), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line",
            quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    plain = captured[0].get_content()
    reply_index = plain.index("Hello")
    footer_index = plain.index("--\nThe Network.")
    quote_index = plain.index("On Sat, 04 Jul 2026 12:00:00 -0700, you wrote:")
    assert reply_index < footer_index < quote_index


def test_append_failure_does_not_propagate():
    """An IMAP append failure must not raise - the SMTP send already succeeded."""
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


def test_send_reply_audits_trace_id_through_smtp_and_imap_append(caplog):
    from thenetwork.email.outbound import send_reply

    trace_id = "d731f003-b5f6-42cf-a490-e3ec29e89c0b"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()), \
         patch("smtplib.SMTP", return_value=_mock_smtp()), \
         patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
            trace_id=trace_id,
        )

    events = _events(caplog)
    correlated = [
        event for event in events
        if event["event"] in {
            "email.smtp_send.started",
            "email.smtp_send.completed",
            "email.imap_append.completed",
        }
    ]
    assert len(correlated) == 3
    assert {event["trace_id"] for event in correlated} == {trace_id}
