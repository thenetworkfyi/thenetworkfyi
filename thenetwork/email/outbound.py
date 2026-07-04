"""SMTP outbound using stdlib email.message.EmailMessage + smtplib over STARTTLS."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from time import monotonic

from imap_tools import MailBox, MailMessageFlags

from thenetwork.audit import audit_event, audit_span
from thenetwork.settings import get_settings

# The one deterministic explanation of what this address is and how to use
# it - injection-proof by construction since it's fixed copy, not model
# output. Sent both for near-empty first contact (worker/tasks.py) and when
# escalate() acknowledges an authenticated sender it can't otherwise help
# (agent/tools.py) - in both cases the sender doesn't yet know how to join.
FIRST_CONTACT_WELCOME_REPLY = """\
To join, reply with a few plain sentences: who you are, what you're
working on, and what kind of person would be worth your time. What you
write is what gets matched on, so specifics help.

Nothing you write is shown to anyone else. When we weigh an
introduction, we work only from an anonymized summary.

You may not hear from us for a while. Introductions happen when they're
warranted, not on a schedule. Silence means the right person hasn't
shown up yet.

--- The Network
"""


def _growth_footer_text(account: str) -> str:
    # "Reply" only reaches us for the direct recipient - a forward's reply
    # goes back to whoever forwarded it, not to us. So the forwarded-to
    # audience needs the address spelled out in plain text, since it has to
    # survive being buried in someone else's quoted thread.
    return f"\n\n--\nThe Network. Reply anytime. Know someone who should be on this? Forward this along - they can join by emailing {account} directly."


def _growth_footer_html(account: str) -> str:
    return (
        "<p style=\"color:#888;font-size:12px\">"
        f"The Network. Reply anytime. Know someone who should be on this? Forward this along "
        f"&mdash; they can join by emailing {account} directly."
        "</p>"
    )


def _append_to_sent(msg: EmailMessage) -> None:
    """Append the just-sent message to the IMAP Sent folder, flagged \\Seen.

    Called only after the SMTP send has already succeeded, so this is
    best-effort visibility (mirroring a normal mail client) rather than part
    of the delivery guarantee: a failure here must not fail the job. Only
    outcome/duration/error-type are audit-logged - never the folder name,
    address, or message content.
    """
    s = get_settings()
    started = monotonic()
    try:
        with MailBox(s.imap_host, s.imap_port).login(s.imap_account, s.imap_password) as mb:
            mb.append(msg.as_bytes(), s.imap_sent_folder, flag_set=[MailMessageFlags.SEEN])
    except Exception as exc:
        audit_event(
            "email.imap_append.completed",
            outcome="error",
            error_type=type(exc).__name__,
            duration_ms=round((monotonic() - started) * 1000, 3),
        )
        return
    audit_event(
        "email.imap_append.completed",
        outcome="success",
        duration_ms=round((monotonic() - started) * 1000, 3),
    )


def notify_admins(settings, subject: str, body: str) -> None:
    """Send an internal ops notification to every configured admin address.

    Shared by `agent/tools.py::escalate` and `agent/core.py`'s
    usage-limit-exceeded handler - both need to alert a human operator
    without routing through the user-facing growth surface, so this wraps
    `send_reply` with `include_footer=False` and a no-op when no admin
    addresses are configured.
    """
    if not settings.admin_emails:
        return
    for admin_email in settings.admin_emails:
        send_reply(
            to_address=admin_email,
            subject=subject,
            body_text=body,
            include_footer=False,
        )


def reply_subject(inbound_subject: str, *, fallback: str) -> str:
    """Return a reply subject, using fallback when the inbound subject is empty."""
    subject = inbound_subject.strip()
    if not subject:
        return fallback
    return f"Re: {subject}"


def send_reply(
    to_address: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    include_footer: bool = True,
) -> None:
    """Send an email from the configured account.

    Uses Auto-Submitted: auto-generated header (RFC 3834) so recipients'
    IMAP pollers skip our outbound replies and don't create a loop.

    The growth footer is appended here, at the mailer level, rather than by
    the agent composing it in `body_text` - that way prompt injection in the
    inbound message can't alter or suppress it. Set include_footer=False for
    internal/ops mail (admin replies, escalation notices) that isn't a
    user-facing growth surface.
    """
    with audit_span(
        "email.smtp_send",
        recipient_id_present=bool(to_address),
        subject_chars=len(subject),
        body_chars=len(body_text),
        html_present=body_html is not None,
    ):
        s = get_settings()

        if include_footer and s.growth_footer_enabled:
            # The footer points new senders at the polled inbound address,
            # not the (possibly different) SMTP sending identity.
            body_text = body_text + _growth_footer_text(s.imap_account)
            if body_html:
                body_html = body_html + _growth_footer_html(s.imap_account)

        msg = EmailMessage()
        msg["From"] = s.email_from
        msg["To"] = to_address
        msg["Subject"] = subject
        # RFC 3834 §3.1.7 - auto-replied for automatic responses to inbound mail
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
            smtp.login(s.smtp_account, s.smtp_password)
            smtp.send_message(msg)

        _append_to_sent(msg)
