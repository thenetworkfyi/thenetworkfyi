"""SMTP outbound using stdlib email.message.EmailMessage + smtplib over STARTTLS."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from thenetwork.settings import get_settings


def send_reply(
    to_address: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    """Send an email from the configured account.

    Uses Auto-Submitted: auto-generated header (RFC 3834) so recipients'
    IMAP pollers skip our outbound replies and don't create a loop.
    """
    s = get_settings()

    msg = EmailMessage()
    msg["From"] = s.email_account
    msg["To"] = to_address
    msg["Subject"] = subject
    # RFC 3834 §3.1.7 — auto-replied for automatic responses to inbound mail
    msg["Auto-Submitted"] = "auto-replied"

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(s.smtp_host, s.smtp_port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(s.email_account, s.email_password)
        smtp.send_message(msg)
