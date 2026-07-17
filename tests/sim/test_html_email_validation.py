from __future__ import annotations

from email.message import EmailMessage
from html import escape

import pytest

from thenetwork.sim.html_validation import (
    assert_html_email_contract,
    inspect_html_email,
)
from thenetwork.sim.run.mail import _extract_body


def _synthetic_multipart_email(*, html: str | None = None) -> EmailMessage:
    token = "[intro:abcdef123456]"
    plain = (
        "Hello Casey,\n\n"
        "Would you like an introduction?\n\n"
        f"{token}\n\n"
        "--\nThe Network\nAn automated connection service\nReply anytime."
    )
    message = EmailMessage()
    message["From"] = "join@example.test"
    message["To"] = "casey@example.test"
    message["Subject"] = "Introduction request"
    message.set_content(plain)
    message.add_alternative(
        html
        or """<html><body><p>Hello Casey,</p><p>Would you like an introduction?</p>
<p>[intro:abcdef123456]</p><hr><p><strong>The Network</strong><br>
An automated connection service<br>Reply anytime.</p></body></html>""",
        subtype="html",
    )
    return message


def test_synthetic_multipart_fixture_has_canonical_plain_text_and_safe_html():
    message = _synthetic_multipart_email()

    inspection = assert_html_email_contract(
        message,
        required_text=(
            "[intro:abcdef123456]",
            "The Network",
            "An automated connection service",
            "Reply anytime.",
        ),
    )

    assert inspection.part_types == ("text/plain", "text/html")
    assert _extract_body(message) == inspection.plain_text
    assert "<" not in _extract_body(message)


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
