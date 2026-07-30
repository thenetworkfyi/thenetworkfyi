"""Unit tests for outbound SMTP send + the post-send IMAP Sent-folder append
(thenetwork/email/outbound.py).

SMTP sends run for real against the in-process aiosmtpd sink provided by the
``smtp_sink`` fixture (tests/conftest.py) rather than being mocked, so header,
MIME-structure, and RFC 3834 assertions below exercise the actual wire path.
IMAP append remains mocked: it is a distinct concern from the outbound SMTP
path this module targets.
"""

from __future__ import annotations

import json
import logging
import smtplib
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from unittest.mock import MagicMock, patch

from imap_tools import MailMessageFlags
import pytest

from thenetwork.audit import LOGGER_NAME
from thenetwork.email.render import RenderedEmail, standard_signature_lines
from thenetwork.sim.html_validation import assert_html_email_contract


def _events(caplog) -> list[dict]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


def _mock_mailbox_success():
    mb_instance = MagicMock()
    mb_instance.__enter__ = MagicMock(return_value=mb_instance)
    mb_instance.__exit__ = MagicMock(return_value=False)
    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.return_value = mb_instance
    return mock_mailbox, mb_instance


def test_append_called_on_success(smtp_sink):
    """After a successful SMTP send, the composed message is appended to IMAP."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    mb_instance.append.assert_called_once()
    assert len(smtp_sink.messages) == 1


def test_admin_notifications_remain_plain_only(smtp_sink):
    from thenetwork.email.outbound import notify_admins
    from thenetwork.settings import get_settings

    smtp_sink.override(admin_emails=["admin@example.com"])
    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        notify_admins(get_settings(), "Operational notice", "Internal detail")

    messages = smtp_sink.messages
    assert len(messages) == 1
    assert not messages[0].is_multipart()
    assert messages[0].get_content() == "Internal detail\n"


def test_send_relay_email_uses_only_server_selected_addresses(caplog, smtp_sink):
    from thenetwork.email.outbound import send_relay_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mock_mailbox, mb_instance = _mock_mailbox_success()
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.com"

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_relay_email(
            to_address="bob@example.com",
            proxy_address=proxy,
            subject="Re: project",
            body_text="Exact extracted body\nwith a second line",
            trace_id="relay-trace",
        )

    messages = smtp_sink.messages
    assert len(messages) == 1
    message = messages[0]
    assert str(message["From"]) == f"The Network <{proxy}>"
    assert str(message["Reply-To"]) == proxy
    assert getaddresses(message.get_all("To", [])) == [("", "bob@example.com")]
    assert str(message["Subject"]) == "Re: project"
    assert message.get_content() == "Exact extracted body\nwith a second line\n"
    assert not message.is_multipart()
    assert "Auto-Submitted" not in message
    assert "agent@example.com" not in message.as_string()
    mb_instance.append.assert_called_once()

    events = _events(caplog)
    relay_events = [event for event in events if event.get("trace_id") == "relay-trace"]
    assert relay_events
    serialized = json.dumps(relay_events)
    assert "bob@example.com" not in serialized
    assert proxy not in serialized
    assert "Exact extracted body" not in serialized
    assert any(
        event.get("template_id") == "introduction_relay" for event in relay_events
    )


def test_send_relay_email_preserves_source_mime_body_and_replaces_headers(smtp_sink):
    from email.message import EmailMessage

    from thenetwork.email.outbound import send_relay_email

    source = EmailMessage()
    source["From"] = "Alice Private <alice.private@example.com>"
    source["To"] = "hidden-source@relay.example.com"
    source["Subject"] = "Sender subject"
    source["Auto-Submitted"] = "auto-generated"
    source.set_content("Plain participant content")
    source.add_alternative(
        "<html><body><p>HTML <strong>participant</strong> content</p></body></html>",
        subtype="html",
    )
    source.add_attachment(
        b"attachment bytes",
        maintype="application",
        subtype="octet-stream",
        filename="notes.bin",
    )
    mock_mailbox, _mb_instance = _mock_mailbox_success()
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.com"

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_relay_email(
            to_address="bob.private@example.com",
            proxy_address=proxy,
            subject="Re: Your introduction",
            body_text="Plain participant content",
            source_message=source.as_bytes(),
        )

    (message,) = smtp_sink.messages
    assert str(message["From"]) == f"The Network <{proxy}>"
    assert str(message["Reply-To"]) == proxy
    assert str(message["To"]) == "bob.private@example.com"
    assert str(message["Subject"]) == "Re: Your introduction"
    assert "Auto-Submitted" not in message
    assert "alice.private@example.com" not in "\n".join(
        str(value) for value in message.values()
    )
    assert message.get_content_type() == "multipart/mixed"
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == (
        "Plain participant content"
    )
    assert (
        "<strong>participant</strong>"
        in message.get_body(preferencelist=("html",)).get_content()
    )
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "notes.bin"
    assert attachment.get_payload(decode=True) == b"attachment bytes"


def test_internal_plain_delivery_is_unsigned_and_audited_separately(caplog, smtp_sink):
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="admin@example.com",
            subject="Internal",
            body_text="Operational detail",
            audience="internal",
        )

    message = smtp_sink.messages[0]
    assert not message.is_multipart()
    assert "The Network" not in message.get_content()
    rendered = [
        event for event in _events(caplog) if event["event"] == "email.rendered"
    ]
    assert len(rendered) == 1
    assert rendered[0]["html_present"] is False
    assert rendered[0]["rendering_mode"] == "internal_plain"


def test_user_delivery_keeps_standard_signature_when_footer_is_disabled(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="recipient@example.com",
            subject="Hi",
            body_text="Body",
            include_footer=False,
        )

    message = smtp_sink.messages[0]
    assert (
        message.get_body(preferencelist=("plain",)).get_content().count("The Network")
        == 1
    )


def test_user_renderer_fallback_is_audited_without_content(caplog, smtp_sink):
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mock_mailbox, _ = _mock_mailbox_success()
    fallback = RenderedEmail(text="safe fallback", html=None)

    with (
        patch(
            "thenetwork.email.outbound.render_conversational_email",
            return_value=fallback,
        ),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="recipient@example.com",
            subject="Hi",
            body_text="private source text",
        )

    rendered = [
        event for event in _events(caplog) if event["event"] == "email.rendered"
    ]
    assert len(rendered) == 1
    assert rendered[0]["html_present"] is False
    assert rendered[0]["rendering_mode"] == "plain_fallback"
    assert "private source text" not in "\n".join(
        record.message for record in caplog.records
    )


def test_fixed_worker_reply_is_multipart_and_preserves_threading(smtp_sink):
    from thenetwork.email.outbound import send_reply
    from thenetwork.email.render import (
        FirstContactWelcomeEmailContext,
        FixedEmailTemplate,
    )

    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="new@example.com",
            subject="How to join",
            fixed_template=FixedEmailTemplate.FIRST_CONTACT_WELCOME,
            fixed_context=FirstContactWelcomeEmailContext(),
            in_reply_to="<original@example.com>",
            references="<root@example.com> <original@example.com>",
        )

    message = smtp_sink.messages[0]
    assert message["In-Reply-To"] == "<original@example.com>"
    assert message["References"] == "<root@example.com> <original@example.com>"
    assert "Welcome," in message.get_body(preferencelist=("plain",)).get_content()
    assert (
        message.get_body(preferencelist=("plain",)).get_content().count("The Network")
        == 1
    )
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]


def test_fixed_worker_reply_rejects_callers_providing_freeform_body_text():
    from thenetwork.email.outbound import send_reply
    from thenetwork.email.render import (
        FirstContactWelcomeEmailContext,
        FixedEmailTemplate,
    )

    with pytest.raises(TypeError, match="do not accept body_text"):
        send_reply(
            to_address="new@example.com",
            subject="How to join",
            body_text="<script>steal()</script>",
            fixed_template=FixedEmailTemplate.FIRST_CONTACT_WELCOME,
            fixed_context=FirstContactWelcomeEmailContext(),
        )


def test_proxy_introduction_sends_one_message_to_each_consented_person(smtp_sink):
    from thenetwork.email.outbound import send_proxy_introduction

    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_proxy_introduction(
            person_a_email="alice@example.com",
            person_b_email="bob@example.com",
            person_a_gist="Builds storage systems",
            person_b_gist="Operates distributed databases",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    messages = smtp_sink.messages
    assert len(messages) == 2
    assert [str(message["To"]) for message in messages] == [
        "alice@example.com",
        "bob@example.com",
    ]
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.com"
    for message in messages:
        assert message["Auto-Submitted"] == "auto-replied"
        assert str(message["From"]) == f"The Network <{proxy}>"
        assert str(message["Reply-To"]) == proxy
        assert getaddresses(message.get_all("To", [])) == [("", str(message["To"]))]
        body = message.get_body(preferencelist=("plain",)).get_content()
        assert "both opted in" in body
        assert "Why you were matched" in body
        assert "Builds storage systems" in body
        assert "Operates distributed databases" in body
        assert "Reply to this message" in body
        assert f"email {proxy} directly" in body
        assert "alice@example.com" not in body
        assert "bob@example.com" not in body
        assert "Alice" not in body
        assert "Bob" not in body


def test_proxy_introduction_audits_rendering_metadata_only(caplog, smtp_sink):
    from thenetwork.email.outbound import send_proxy_introduction

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_proxy_introduction(
            person_a_email="alice@example.com",
            person_b_email="bob@example.com",
            person_a_gist="Builds storage systems",
            person_b_gist="Operates distributed databases",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    smtp_events = [
        event
        for event in _events(caplog)
        if event["event"] in {"email.smtp_send.started", "email.smtp_send.completed"}
    ]
    assert len(smtp_events) == 4
    assert {event["recipient_count"] for event in smtp_events} == {1}
    assert {event["template_id"] for event in smtp_events} == {"introduction"}
    assert all("body_chars" not in event for event in smtp_events)
    assert all("subject_chars" not in event for event in smtp_events)

    rendered_events = [
        event for event in _events(caplog) if event["event"] == "email.rendered"
    ]
    assert len(rendered_events) == 2
    assert {
        "html_present": True,
        "outcome": "success",
        "recipient_count": 1,
        "template_id": "introduction",
    }.items() <= rendered_events[0].items()
    serialized = json.dumps(_events(caplog))
    assert "alice@example.com" not in serialized
    assert "bob@example.com" not in serialized


def test_proxy_introduction_messages_preserve_valid_multipart_alternatives(smtp_sink):
    from thenetwork.email.outbound import send_proxy_introduction

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_proxy_introduction(
            person_a_email="alice@example.com",
            person_b_email="bob@example.com",
            person_a_gist="Builds storage systems",
            person_b_gist="Operates distributed databases",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    messages = smtp_sink.messages
    assert len(messages) == 2
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.com"
    for message in messages:
        signature_address = standard_signature_lines()[1]
        inspection = assert_html_email_contract(
            message,
            required_text=(
                proxy,
                "The Network",
                signature_address,
            ),
        )
        assert inspection.part_types == ("text/plain", "text/html")


def test_event_fyi_uses_fixed_subject_template_and_standard_signature(smtp_sink):
    from thenetwork.email.outbound import EVENT_RECOMMENDATION_SUBJECT, send_event_fyi
    from thenetwork.email.render import EventRecommendationNotice

    smtp_sink.override(growth_footer_enabled=True)
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_event_fyi(
            to_address="recipient@example.com",
            event_gist="A sealed event gist",
            notice=EventRecommendationNotice.FIRST,
        )

    message = smtp_sink.messages[0]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert message["Subject"] == EVENT_RECOMMENDATION_SUBJECT
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    signature_address = standard_signature_lines()[1]
    for body in (plain, unescape(html)):
        assert body.count("A sealed event gist") == 1
        assert body.count(EventRecommendationNotice.FIRST.value) == 1
        assert body.count("The Network") == 1
        assert body.count(signature_address) == 1
        assert "agent@example.com" not in body


def test_send_reply_uses_short_standard_signature(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(to_address="recipient@example.com", subject="Hi", body_text="Body")

    body = smtp_sink.messages[0].get_body(preferencelist=("plain",)).get_content()
    signature_address = standard_signature_lines()[1]
    assert body.count("The Network") == 1
    assert body.count(signature_address) == 1
    assert body.endswith(f"The Network\n{signature_address}\n")
    for removed_text in (
        "An automated connection service",
        "Reply anytime.",
        "Know someone who should be on this?",
        "Forward this along",
        "agent@example.com",
    ):
        assert removed_text not in body


def test_append_uses_configured_folder_name(smtp_sink):
    """The folder passed to append() comes from settings.imap_sent_folder."""
    from thenetwork.email.outbound import send_reply

    smtp_sink.override(imap_sent_folder="MyCustomSent")
    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    args, kwargs = mb_instance.append.call_args
    folder = args[1] if len(args) > 1 else kwargs.get("folder")
    assert folder == "MyCustomSent"


def test_append_sets_seen_flag(smtp_sink):
    """The appended message must be flagged \\Seen so it doesn't show as unread."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    _, kwargs = mb_instance.append.call_args
    assert kwargs.get("flag_set") == [MailMessageFlags.SEEN]


def test_append_receives_exact_composed_message(smtp_sink):
    """The message appended to IMAP must be the same one that was SMTP-sent."""
    from email import policy as email_policy
    from email.parser import BytesParser

    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    args, _ = mb_instance.append.call_args
    appended = BytesParser(policy=email_policy.default).parsebytes(args[0])
    sent = smtp_sink.messages[0]
    # Each serialization of a multipart message re-randomizes its MIME
    # boundary, so raw bytes aren't expected to match; compare the message
    # content and identifying headers instead.
    assert appended["Subject"] == sent["Subject"]
    assert appended["Message-ID"] == sent["Message-ID"]
    assert appended["Date"] == sent["Date"]
    assert (
        appended.get_body(preferencelist=("plain",)).get_content()
        == sent.get_body(preferencelist=("plain",)).get_content()
    )


def test_send_reply_sets_date_and_message_id_headers(smtp_sink):
    """The built message must carry parseable Date/Message-ID headers so the
    SMTP-sent copy and the IMAP Sent append (verbatim bytes of the same
    message) both show a date and can thread, instead of relying on the
    submission MTA to stamp Date."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    sent_msg = smtp_sink.messages[0]
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


def test_send_reply_appends_plain_text_quoted_trail(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line\n> old quote\nSecond line",
            quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
            include_footer=False,
        )

    msg = smtp_sink.messages[0]
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


def test_send_reply_escapes_html_quoted_trail(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="<script>steal()</script>",
            include_footer=False,
        )

    html = smtp_sink.messages[0].get_body(preferencelist=("html",)).get_content()
    assert "<script>steal()</script>" not in html
    assert "&lt;script&gt;steal()&lt;/script&gt;" in html


def test_send_reply_quote_is_in_both_user_facing_alternatives(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line",
            include_footer=False,
        )

    msg = smtp_sink.messages[0]
    assert msg.get_content_type() == "multipart/alternative"
    for part in msg.iter_parts():
        assert "On an earlier message, you wrote:" in part.get_content()


def test_send_reply_places_signature_before_quoted_trail(smtp_sink):
    from thenetwork.email.outbound import send_reply

    smtp_sink.override(growth_footer_enabled=True)
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line",
            quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    plain = smtp_sink.messages[0].get_body(preferencelist=("plain",)).get_content()
    reply_index = plain.index("Hello")
    footer_index = plain.index("\n".join(standard_signature_lines()))
    quote_index = plain.index("On Sat, 04 Jul 2026 12:00:00 -0700, you wrote:")
    assert reply_index < footer_index < quote_index


def test_send_reply_builds_plain_first_multipart_alternative(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    message = smtp_sink.messages[0]
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert message["Message-ID"]
    assert message["Auto-Submitted"] == "auto-replied"


def test_send_reply_render_fallback_sends_complete_plain_only_message(smtp_sink):
    from thenetwork.email.outbound import send_reply

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch(
            "thenetwork.email.outbound.render_conversational_email",
            return_value=RenderedEmail(text="Complete plain message", html=None),
        ),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    message = smtp_sink.messages[0]
    assert not message.is_multipart()
    assert message.get_content() == "Complete plain message\n"


def test_append_failure_does_not_propagate(smtp_sink):
    """An IMAP append failure must not raise - the SMTP send already succeeded."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.side_effect = OSError("connection refused")

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        # Must not raise.
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    assert len(smtp_sink.messages) == 1


def test_append_failure_is_audit_logged_as_error(caplog, smtp_sink):
    """A failed append is audit-logged with outcome=error, no folder/address/content."""
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.side_effect = OSError("connection refused")

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    events = _events(caplog)
    append_events = [
        e for e in events if e.get("event") == "email.imap_append.completed"
    ]
    assert len(append_events) == 1
    assert append_events[0]["outcome"] == "error"
    assert append_events[0]["error_type"] == "OSError"

    serialized = "\n".join(record.message for record in caplog.records)
    assert "MyCustomSent" not in serialized
    assert "Sent" not in serialized
    assert "bob@example.com" not in serialized
    assert "Hello" not in serialized


def test_append_success_is_audit_logged(caplog, smtp_sink):
    """A successful append is audit-logged with outcome=success."""
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    events = _events(caplog)
    append_events = [
        e for e in events if e.get("event") == "email.imap_append.completed"
    ]
    assert len(append_events) == 1
    assert append_events[0]["outcome"] == "success"

    serialized = "\n".join(record.message for record in caplog.records)
    assert "bob@example.com" not in serialized
    assert "Hello" not in serialized


def test_send_reply_audits_trace_id_through_smtp_and_imap_append(caplog, smtp_sink):
    from thenetwork.email.outbound import send_reply

    trace_id = "d731f003-b5f6-42cf-a490-e3ec29e89c0b"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
            trace_id=trace_id,
        )

    events = _events(caplog)
    correlated = [
        event
        for event in events
        if event["event"]
        in {
            "email.smtp_send.started",
            "email.smtp_send.completed",
            "email.imap_append.completed",
        }
    ]
    assert len(correlated) == 3
    assert {event["trace_id"] for event in correlated} == {trace_id}


def test_smtp_sends_cannot_reach_the_deployment_mta(smtp_sink, monkeypatch):
    """Guard: the sink fixture must be what every send actually contacts.

    thenetwork.settings.Settings.smtp_host defaults to smtp.gmail.com for a
    real deployment. If a test somehow left that default in place, this would
    either hang attempting a real network connection or fail a DNS/connect
    error - never silently succeed - because SMTP.connect is asserted to only
    ever target the sink's own host.
    """
    from thenetwork.email.outbound import send_reply
    from thenetwork.settings import Settings, get_settings

    assert Settings.model_fields["smtp_host"].default == "smtp.gmail.com"
    assert smtp_sink.host != "smtp.gmail.com"
    assert get_settings().smtp_host == smtp_sink.host
    assert get_settings().smtp_host != "smtp.gmail.com"

    original_connect = smtplib.SMTP.connect

    def _guarded_connect(self, host="", port=0, *args, **kwargs):
        assert host == smtp_sink.host, (
            f"SMTP attempted to connect to unexpected host {host!r} "
            f"instead of the test sink {smtp_sink.host!r}"
        )
        return original_connect(self, host, port, *args, **kwargs)

    monkeypatch.setattr(smtplib.SMTP, "connect", _guarded_connect)
    mock_mailbox, _ = _mock_mailbox_success()

    with patch("thenetwork.email.outbound.MailBox", mock_mailbox):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    assert len(smtp_sink.messages) == 1
