"""Unit tests for the post-send IMAP Sent-folder append (thenetwork/email/outbound.py)."""

from __future__ import annotations

import json
import logging
from email.utils import getaddresses, parsedate_to_datetime
from unittest.mock import MagicMock, patch

from imap_tools import MailMessageFlags
import pytest

from thenetwork.audit import LOGGER_NAME
from thenetwork.email.render import RenderedEmail


def _events(caplog) -> list[dict]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


def _mock_settings(**overrides):
    s = MagicMock()
    s.smtp_host = "smtp.example.com"
    s.smtp_port = 587
    s.imap_account = "agent@example.com"
    s.imap_password = "secret"
    s.smtp_account = "agent@example.com"
    s.smtp_password = "secret"
    s.email_from = "agent@example.com"
    s.relay_domain = "relay.example.com"
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

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    mb_instance.append.assert_called_once()


def test_admin_notifications_remain_plain_only():
    from thenetwork.email.outbound import notify_admins

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _ = _mock_mailbox_success()
    settings = _mock_settings(admin_emails=["admin@example.com"])

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=settings),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        notify_admins(settings, "Operational notice", "Internal detail")

    assert len(captured) == 1
    assert not captured[0].is_multipart()
    assert captured[0].get_content() == "Internal detail\n"


def test_send_relay_email_uses_only_server_selected_addresses(caplog):
    from thenetwork.email.outbound import send_relay_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, mb_instance = _mock_mailbox_success()
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.com"

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_relay_email(
            to_address="bob@example.com",
            proxy_address=proxy,
            subject="Re: project",
            body_text="Exact extracted body\nwith a second line",
            trace_id="relay-trace",
        )

    assert len(captured) == 1
    message = captured[0]
    assert str(message["From"]) == f"The Network <{proxy}>"
    assert str(message["Reply-To"]) == proxy
    assert getaddresses(message.get_all("To", [])) == [("", "bob@example.com")]
    assert str(message["Subject"]) == "Re: project"
    assert message.get_content() == "Exact extracted body\nwith a second line\n"
    assert not message.is_multipart()
    assert "Auto-Submitted" not in message
    assert "agent@example.com" not in message.as_string()
    mb_instance.append.assert_called_once()
    assert mb_instance.append.call_args.args[0] == message.as_bytes()

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


def test_internal_plain_delivery_is_unsigned_and_audited_separately(caplog):
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _ = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="admin@example.com",
            subject="Internal",
            body_text="Operational detail",
            audience="internal",
        )

    assert not captured[0].is_multipart()
    assert "The Network" not in captured[0].get_content()
    rendered = [
        event for event in _events(caplog) if event["event"] == "email.rendered"
    ]
    assert len(rendered) == 1
    assert rendered[0]["html_present"] is False
    assert rendered[0]["rendering_mode"] == "internal_plain"


def test_user_delivery_keeps_standard_signature_when_footer_is_disabled():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _ = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="recipient@example.com",
            subject="Hi",
            body_text="Body",
            include_footer=False,
        )

    assert (
        captured[0]
        .get_body(preferencelist=("plain",))
        .get_content()
        .count("The Network")
        == 1
    )


def test_user_renderer_fallback_is_audited_without_content(caplog):
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mock_mailbox, _ = _mock_mailbox_success()
    fallback = RenderedEmail(text="safe fallback", html=None)

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch(
            "thenetwork.email.outbound.render_conversational_email",
            return_value=fallback,
        ),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
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


def test_fixed_worker_reply_is_multipart_and_preserves_threading():
    from thenetwork.email.outbound import send_reply
    from thenetwork.email.render import (
        FirstContactWelcomeEmailContext,
        FixedEmailTemplate,
    )

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _ = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="new@example.com",
            subject="How to join",
            fixed_template=FixedEmailTemplate.FIRST_CONTACT_WELCOME,
            fixed_context=FirstContactWelcomeEmailContext(),
            in_reply_to="<original@example.com>",
            references="<root@example.com> <original@example.com>",
        )

    message = captured[0]
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


def test_proxy_introduction_sends_one_message_to_each_consented_person():
    from thenetwork.email.outbound import send_proxy_introduction

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)
    mock_mailbox, _ = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_proxy_introduction(
            person_a_name="Alice",
            person_a_email="alice@example.com",
            person_b_name="Bob",
            person_b_email="bob@example.com",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    assert len(captured) == 2
    assert [str(message["To"]) for message in captured] == [
        "alice@example.com",
        "bob@example.com",
    ]
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.com"
    for message in captured:
        assert str(message["From"]) == f"The Network <{proxy}>"
        assert str(message["Reply-To"]) == proxy
        assert getaddresses(message.get_all("To", [])) == [("", str(message["To"]))]
        body = message.get_content()
        assert "Alice and Bob" in body
        assert "both opted in" in body
        assert "Reply to this message" in body
        assert "addresses are included" not in body
        assert "alice@example.com" not in body
        assert "bob@example.com" not in body


def test_proxy_introduction_audits_rendering_metadata_only(caplog):
    from thenetwork.email.outbound import send_proxy_introduction

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mock_mailbox, _ = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_proxy_introduction(
            person_a_name="Alice",
            person_a_email="alice@example.com",
            person_b_name="Bob",
            person_b_email="bob@example.com",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    smtp_events = [
        event
        for event in _events(caplog)
        if event["event"] in {"email.smtp_send.started", "email.smtp_send.completed"}
    ]
    assert len(smtp_events) == 4
    assert {event["recipient_count"] for event in smtp_events} == {1}
    assert {event["template_id"] for event in smtp_events} == {"introduction_relay"}
    assert all("body_chars" not in event for event in smtp_events)
    assert all("subject_chars" not in event for event in smtp_events)

    rendered_events = [
        event for event in _events(caplog) if event["event"] == "email.rendered"
    ]
    assert len(rendered_events) == 2
    assert {
        "html_present": False,
        "outcome": "success",
        "recipient_count": 1,
        "template_id": "introduction_relay",
    }.items() <= rendered_events[0].items()
    serialized = json.dumps(_events(caplog))
    assert "alice@example.com" not in serialized
    assert "bob@example.com" not in serialized


def test_proxy_introduction_messages_are_plain_only():
    from thenetwork.email.outbound import send_proxy_introduction

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_proxy_introduction(
            person_a_name="Alice",
            person_a_email="alice@example.com",
            person_b_name="Bob",
            person_b_email="bob@example.com",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    assert len(captured) == 2
    assert all(not message.is_multipart() for message in captured)


def test_event_fyi_uses_fixed_subject_template_and_one_referral_signature():
    from thenetwork.email.outbound import EVENT_RECOMMENDATION_SUBJECT, send_event_fyi
    from thenetwork.email.render import EventRecommendationNotice

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch(
            "thenetwork.email.outbound.get_settings",
            return_value=_mock_settings(growth_footer_enabled=True),
        ),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_event_fyi(
            to_address="recipient@example.com",
            event_gist="A sealed event gist",
            notice=EventRecommendationNotice.FIRST,
        )

    message = captured[0]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert message["Subject"] == EVENT_RECOMMENDATION_SUBJECT
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    for body in (plain, html):
        assert body.count("A sealed event gist") == 1
        assert body.count(EventRecommendationNotice.FIRST.value) == 1
        assert body.count("The Network") == 1
        assert body.count("agent@example.com") == 1


def test_send_reply_without_referral_keeps_standard_signature():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(to_address="recipient@example.com", subject="Hi", body_text="Body")

    body = captured[0].get_body(preferencelist=("plain",)).get_content()
    assert body.count("The Network") == 1
    assert "Know someone who should be on this?" not in body


def test_append_uses_configured_folder_name():
    """The folder passed to append() comes from settings.imap_sent_folder."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with (
        patch(
            "thenetwork.email.outbound.get_settings",
            return_value=_mock_settings(imap_sent_folder="MyCustomSent"),
        ),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    args, kwargs = mb_instance.append.call_args
    folder = args[1] if len(args) > 1 else kwargs.get("folder")
    assert folder == "MyCustomSent"


def test_append_sets_seen_flag():
    """The appended message must be flagged \\Seen so it doesn't show as unread."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    _, kwargs = mb_instance.append.call_args
    assert kwargs.get("flag_set") == [MailMessageFlags.SEEN]


def test_append_receives_exact_composed_message():
    """The bytes appended must be the same message that was SMTP-sent."""
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

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

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

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

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
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

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="<script>steal()</script>",
            include_footer=False,
        )

    html = captured[0].get_body(preferencelist=("html",)).get_content()
    assert "<script>steal()</script>" not in html
    assert "&lt;script&gt;steal()&lt;/script&gt;" in html


def test_send_reply_quote_is_in_both_user_facing_alternatives():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line",
            include_footer=False,
        )

    msg = captured[0]
    assert msg.get_content_type() == "multipart/alternative"
    for part in msg.iter_parts():
        assert "On an earlier message, you wrote:" in part.get_content()


def test_send_reply_places_growth_footer_before_quoted_trail():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = lambda msg: captured.append(msg)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch(
            "thenetwork.email.outbound.get_settings",
            return_value=_mock_settings(growth_footer_enabled=True),
        ),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            quoted_body_text="Original line",
            quoted_date="Sat, 04 Jul 2026 12:00:00 -0700",
        )

    plain = captured[0].get_body(preferencelist=("plain",)).get_content()
    reply_index = plain.index("Hello")
    footer_index = plain.index("--\nThe Network\nAn automated connection service")
    quote_index = plain.index("On Sat, 04 Jul 2026 12:00:00 -0700, you wrote:")
    assert reply_index < footer_index < quote_index


def test_send_reply_builds_plain_first_multipart_alternative():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    message = captured[0]
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert message["Message-ID"]
    assert message["Auto-Submitted"] == "auto-replied"


def test_send_reply_render_fallback_sends_complete_plain_only_message():
    from thenetwork.email.outbound import send_reply

    captured = []
    smtp_instance = _mock_smtp()
    smtp_instance.send_message.side_effect = captured.append
    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch(
            "thenetwork.email.outbound.render_conversational_email",
            return_value=RenderedEmail(text="Complete plain message", html=None),
        ),
        patch("smtplib.SMTP", return_value=smtp_instance),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )

    message = captured[0]
    assert not message.is_multipart()
    assert message.get_content() == "Complete plain message\n"


def test_append_failure_does_not_propagate():
    """An IMAP append failure must not raise - the SMTP send already succeeded."""
    from thenetwork.email.outbound import send_reply

    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.side_effect = OSError("connection refused")

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
        # Must not raise.
        send_reply(
            to_address="bob@example.com",
            subject="Hi",
            body_text="Hello",
            include_footer=False,
        )


def test_append_failure_is_audit_logged_as_error(caplog):
    """A failed append is audit-logged with outcome=error, no folder/address/content."""
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox = MagicMock()
    mock_mailbox.return_value.login.side_effect = OSError("connection refused")

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
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


def test_append_success_is_audit_logged(caplog):
    """A successful append is audit-logged with outcome=success."""
    from thenetwork.email.outbound import send_reply

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
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


def test_send_reply_audits_trace_id_through_smtp_and_imap_append(caplog):
    from thenetwork.email.outbound import send_reply

    trace_id = "d731f003-b5f6-42cf-a490-e3ec29e89c0b"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    mock_mailbox, _mb_instance = _mock_mailbox_success()

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=_mock_settings()),
        patch("smtplib.SMTP", return_value=_mock_smtp()),
        patch("thenetwork.email.outbound.MailBox", mock_mailbox),
    ):
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
