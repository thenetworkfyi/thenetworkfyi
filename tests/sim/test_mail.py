from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.sim.mail import SimPostOffice, capture_outbound, deliver_inbound


def test_post_office_captures_send_reply_at_smtp_seam():
    from thenetwork.email.outbound import send_reply

    settings = MagicMock()
    settings.smtp_host = "smtp.example.com"
    settings.smtp_port = 587
    settings.smtp_account = "agent@example.com"
    settings.smtp_password = "secret"
    settings.email_from = "agent@example.com"
    settings.imap_account = "join@example.com"
    settings.growth_footer_enabled = True
    post_office = SimPostOffice()

    with patch("thenetwork.email.outbound.get_settings", return_value=settings), capture_outbound(post_office):
        send_reply(
            to_address="Casey@Example.COM",
            subject="Intro",
            body_text="Useful overlap here.",
            in_reply_to="<inbound@example.com>",
            references="<root@example.com>",
        )

    (captured,) = post_office.messages_for("casey@example.com")
    assert captured["From"] == "agent@example.com"
    assert captured["To"] == "Casey@Example.COM"
    assert captured["Subject"] == "Intro"
    assert captured["In-Reply-To"] == "<inbound@example.com>"
    assert captured["References"] == "<root@example.com>"
    assert captured["Auto-Submitted"] == "auto-replied"
    assert captured["Message-ID"]
    assert "The Network. Reply anytime." in captured.get_content()


@pytest.mark.asyncio
async def test_deliver_inbound_calls_process_email_func_with_threading_fields():
    message = EmailMessage()
    message["From"] = "Priya Shah <priya@example.com>"
    message["To"] = "join@example.com"
    message["Subject"] = "Looking for infra people"
    message["Message-ID"] = "<msg-1@example.com>"
    message["References"] = "<root@example.com>"
    message["Date"] = "Sat, 04 Jul 2026 12:00:00 -0700"
    message.set_content("I work on ML infrastructure.")
    process = AsyncMock()

    delivery = await deliver_inbound(
        message,
        sender_authenticated=True,
        process=process,
        trace_id="399005c4-1494-4c94-bc5c-cc1036666679",
    )

    process.assert_awaited_once_with(
        sender_email="priya@example.com",
        subject="Looking for infra people",
        body="I work on ML infrastructure.\n",
        sender_authenticated=True,
        sender_display_name="Priya Shah",
        raw_message_b64=None,
        inbound_message_id="<msg-1@example.com>",
        inbound_references="<root@example.com>",
        inbound_body_for_quote="I work on ML infrastructure.\n",
        inbound_date="Sat, 04 Jul 2026 12:00:00 -0700",
        trace_id="399005c4-1494-4c94-bc5c-cc1036666679",
    )
    assert delivery.sender_email == "priya@example.com"
    assert delivery.trace_id == "399005c4-1494-4c94-bc5c-cc1036666679"


@pytest.mark.asyncio
async def test_deliver_inbound_extracts_html_when_plain_text_is_missing():
    message = EmailMessage()
    message["From"] = "sam@example.com"
    message["Subject"] = "Hello"
    message.add_alternative("<html><body><p>Hello <b>there</b></p></body></html>", subtype="html")
    process = AsyncMock()

    await deliver_inbound(message, process=process)

    assert process.await_args.kwargs["body"] == "Hello there"


@pytest.mark.asyncio
async def test_deliver_inbound_rejects_messages_without_sender():
    message = EmailMessage()
    message["Subject"] = "No sender"
    message.set_content("body")

    with pytest.raises(ValueError, match="From address"):
        await deliver_inbound(message, process=AsyncMock())

