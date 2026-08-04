from __future__ import annotations

from email.message import EmailMessage
from html import escape
from unittest.mock import MagicMock, patch

import pytest

from thenetwork.email.render import standard_signature_lines
from thenetwork.sim.html_validation import (
    assert_html_email_contract,
    inspect_html_email,
)
from thenetwork.sim.run.mail import _extract_body


def _synthetic_multipart_email(*, html: str | None = None) -> EmailMessage:
    token = "[intro:abcdef123456]"
    signature_name, signature_address = standard_signature_lines()
    plain = (
        "Hello Casey,\n\n"
        "Would you like an introduction?\n\n"
        f"{token}\n\n"
        f"{signature_name}\n{signature_address}"
    )
    message = EmailMessage()
    message["From"] = "join@example.test"
    message["To"] = "casey@example.test"
    message["Subject"] = "Introduction request"
    message.set_content(plain)
    message.add_alternative(
        html
        or f"""<html><body><p>Hello Casey,</p><p>Would you like an introduction?</p>
<p>[intro:abcdef123456]</p><hr><p><strong>The Network</strong><br>
{signature_address}</p></body></html>""",
        subtype="html",
    )
    return message


def _production_conversational_multipart() -> EmailMessage:
    from thenetwork.email.outbound import send_reply

    captured: list[EmailMessage] = []
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.__exit__.return_value = False
    smtp.send_message.side_effect = captured.append
    mailbox = MagicMock()
    mailbox.return_value.login.return_value.__enter__.return_value = MagicMock()
    settings = MagicMock(
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_account="agent@example.test",
        smtp_password="secret",
        email_from="agent@example.test",
        imap_host="imap.example.test",
        imap_port=993,
        imap_account="join@example.test",
        imap_password="secret",
        imap_sent_folder="Sent",
    )

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=settings),
        patch("thenetwork.email.outbound.smtplib.SMTP", return_value=smtp),
        patch("thenetwork.email.outbound.MailBox", mailbox),
    ):
        send_reply(
            to_address="casey@example.test",
            subject="A possible connection",
            body_text="Would you like an introduction?",
            quoted_body_text="Original note\nSecond original line",
            quoted_date="Tuesday",
        )

    assert len(captured) == 1
    return captured[0]


def test_synthetic_multipart_fixture_has_canonical_plain_text_and_safe_html():
    message = _synthetic_multipart_email()

    inspection = assert_html_email_contract(
        message,
        required_text=(
            "[intro:abcdef123456]",
            *standard_signature_lines(),
        ),
    )

    assert inspection.part_types == ("text/plain", "text/html")
    assert _extract_body(message) == inspection.plain_text
    assert "<" not in _extract_body(message)


def test_production_conversational_mime_with_quote_passes_contract():
    message = _production_conversational_multipart()

    inspection = assert_html_email_contract(
        message,
        required_text=(
            "Would you like an introduction?",
            *standard_signature_lines(),
            "On Tuesday, you wrote:",
            "Original note",
            "Second original line",
        ),
    )

    assert inspection.part_types == ("text/plain", "text/html")
    assert inspection.plain_text is not None
    assert "> Original note" in inspection.plain_text
    assert inspection.html is not None
    assert "<style>" in inspection.html
    assert (
        "<blockquote>Original note<br>Second original line</blockquote>"
        in inspection.html
    )
    assert _extract_body(message) == inspection.plain_text


@pytest.mark.parametrize(
    ("html", "expected_violation"),
    [
        ("<p>Hello</p><script>alert(1)</script>", "forbidden HTML element: script"),
        (
            "<p>Hello</p><img src='https://example.test/pixel.png'>",
            "forbidden HTML element: img",
        ),
        ("<p onclick='steal()'>Hello</p>", "event handler attribute: onclick"),
        (
            "<form action='https://example.test'><input></form>",
            "forbidden HTML element: form",
        ),
        ("<p hidden>Hidden</p>", "hidden content attribute: hidden"),
        (
            "<html><head><style>@import url('https://example.test/mail.css');</style>"
            "</head><body><p>Hello Casey,</p></body></html>",
            "remote stylesheet import",
        ),
        (
            "<html><head><style>.message { display: none; }</style></head>"
            "<body><p>Hello Casey,</p></body></html>",
            "hidden content style",
        ),
    ],
)
def test_synthetic_fixture_rejects_unsafe_html(html, expected_violation):
    inspection = inspect_html_email(_synthetic_multipart_email(html=html))

    assert expected_violation in inspection.violations


def test_synthetic_fixture_detects_unescaped_input():
    untrusted = "<Casey & Co>"
    message = _synthetic_multipart_email(html=f"<p>{untrusted}</p>")

    inspection = inspect_html_email(message, untrusted_values=(untrusted,))

    assert "unescaped fixture input appears in HTML" in inspection.violations
    assert (
        escape(untrusted)
        not in message.get_body(preferencelist=("html",)).get_content()
    )


def test_fixture_rejects_real_semantic_drift():
    inspection = inspect_html_email(
        _synthetic_multipart_email(
            html="<html><body><p>This says something else.</p></body></html>"
        )
    )

    assert "plain text and visible HTML text differ" in inspection.violations


def test_fixture_rejects_ordinary_leading_quote_marker_drift():
    message = EmailMessage()
    message.set_content(
        "> ordinary canonical body text\n\n"
        "A second paragraph.\n\n"
        "On Tuesday, you wrote:\n"
        "> quoted trailing text"
    )
    message.add_alternative(
        "<p>ordinary canonical body text</p><p>A second paragraph.</p>"
        "<p>On Tuesday, you wrote:</p><blockquote>quoted trailing text</blockquote>",
        subtype="html",
    )

    inspection = inspect_html_email(message)

    assert "plain text and visible HTML text differ" in inspection.violations
