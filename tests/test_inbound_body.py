"""Tests for the attachment-free inbound email body boundary."""
from __future__ import annotations

from email.message import EmailMessage

import pytest

from thenetwork.email.inbound import (
    MAX_BODY_CHARS,
    MAX_RAW_BODY_CHARS,
    BodyTooLargeError,
    extract_body,
)


def test_plain_text_body_is_extracted_without_binary_attachment():
    message = EmailMessage()
    message.set_content("body only")
    message.add_attachment(b"secret attachment", maintype="application", subtype="octet-stream")

    assert extract_body(message) == "body only\n"


def test_text_attachment_without_filename_is_ignored():
    message = EmailMessage()
    message.set_content("body only")

    attachment = EmailMessage()
    attachment.set_content("attachment payload")
    attachment["Content-Disposition"] = "attachment"
    message.make_mixed()
    message.attach(attachment)

    assert extract_body(message) == "body only\n"


def test_attached_email_body_is_not_traversed():
    message = EmailMessage()
    message.set_content("outer body")

    forwarded = EmailMessage()
    forwarded.set_content("forwarded secret")
    message.add_attachment(forwarded)

    assert extract_body(message) == "outer body\n"


def test_filename_marks_text_part_as_attachment_even_when_inline():
    message = EmailMessage()
    message.set_content("body only")
    message.add_attachment("attachment payload", filename="notes.txt")

    assert extract_body(message) == "body only\n"


def test_content_id_marks_inline_text_as_an_attachment():
    message = EmailMessage()
    message.set_content("body only")

    attachment = EmailMessage()
    attachment.set_content("inline attachment payload")
    attachment["Content-ID"] = "<embedded-text>"
    message.make_mixed()
    message.attach(attachment)

    assert extract_body(message) == "body only\n"


def test_plain_text_is_preferred_over_html_alternative():
    message = EmailMessage()
    message.set_content("plain body")
    message.add_alternative("<p>HTML body</p>", subtype="html")

    assert extract_body(message) == "plain body\n"


def test_html_only_body_is_reduced_to_visible_text():
    message = EmailMessage()
    message.set_content(
        "<html><head><title>hidden</title><style>.x { color: red }</style></head>"
        "<body><p>Hello <b>there</b></p><script>steal()</script></body></html>",
        subtype="html",
    )

    assert extract_body(message) == "Hello there"


def test_body_is_bounded_before_it_reaches_the_agent():
    message = EmailMessage()
    message.set_content("a" * (MAX_BODY_CHARS + 100))

    assert extract_body(message) == "a" * MAX_BODY_CHARS


def test_absurdly_large_body_is_rejected_instead_of_truncated():
    message = EmailMessage()
    message.set_content("a" * (MAX_RAW_BODY_CHARS + 1))

    with pytest.raises(BodyTooLargeError) as exc_info:
        extract_body(message)

    assert exc_info.value.body_chars > MAX_RAW_BODY_CHARS
