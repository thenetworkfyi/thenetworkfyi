"""SMTP outbound using stdlib email.message.EmailMessage + smtplib over STARTTLS."""
from __future__ import annotations

from html import escape
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from time import monotonic

from imap_tools import MailBox, MailMessageFlags

from thenetwork.audit import audit_event, audit_span, audit_trace
from thenetwork.email.threading import clean_message_id, clean_references
from thenetwork.settings import get_settings

# The one deterministic explanation of what this address is and how to use
# it - injection-proof by construction since it's fixed copy, not model
# output. Sent both for near-empty first contact (worker/tasks.py) and when
# escalate() routes an authenticated first contact to a standard welcome
# instead of human escalation (agent/tools.py) - in both cases the sender
# doesn't yet know how to join.
FIRST_CONTACT_WELCOME_REPLY = """\
Welcome,

To join, let us know something about yourself, what might be interesting
to you and/or what you're working on. What you send is what gets matched
on. Specifics help, but don't feel like you need to write a long essay.
A few sentences are more than enough to get started.

You may not hear from us for a while. Introductions happen when they
make sense, not on a schedule. Silence means the right thing has yet
to present itself.
"""

MAX_QUOTED_TRAIL_CHARS = 2_000


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


def _quoted_body_lines(body_text: str) -> tuple[list[str], bool]:
    body = body_text.replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith(">"))
    truncated = len(body) > MAX_QUOTED_TRAIL_CHARS
    if truncated:
        body = body[:MAX_QUOTED_TRAIL_CHARS].rstrip()
    return body.splitlines(), truncated


def _quoted_trail_text(body_text: str, quoted_date: str | None = None) -> str:
    """Return a one-level plain-text quote of the original inbound body."""
    body_lines, truncated = _quoted_body_lines(body_text)
    date = quoted_date or "an earlier message"
    quote_lines = [f"On {date}, you wrote:"]
    quote_lines.extend(f"> {line}" if line else ">" for line in body_lines)
    if truncated:
        quote_lines.append("> [quoted text truncated]")
    return "\n\n" + "\n".join(quote_lines)


def _quoted_trail_html(body_text: str, quoted_date: str | None = None) -> str:
    """Return an escaped one-level HTML quote of the original inbound body."""
    body_lines, truncated = _quoted_body_lines(body_text)
    if truncated:
        body_lines.append("[quoted text truncated]")
    date = escape(quoted_date or "an earlier message")
    quote = "<br>\n".join(
        escape(line) if line else "<br>" for line in body_lines)
    return f"\n\n<p>On {date}, you wrote:</p><blockquote>{quote}</blockquote>"


def _append_to_sent(msg: EmailMessage, trace_id: str | None = None) -> None:
    """Append the just-sent message to the IMAP Sent folder, flagged \\Seen.

    Called only after the SMTP send has already succeeded, so this is
    best-effort visibility (mirroring a normal mail client) rather than part
    of the delivery guarantee: a failure here must not fail the job. Only
    outcome/duration/error-type are audit-logged - never the folder name,
    address, or message content.
    """
    with audit_trace(trace_id):
        s = get_settings()
        started = monotonic()
        try:
            with MailBox(s.imap_host, s.imap_port).login(s.imap_account, s.imap_password) as mb:
                mb.append(msg.as_bytes(), s.imap_sent_folder,
                          flag_set=[MailMessageFlags.SEEN])
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


def notify_admins(
    settings,
    subject: str,
    body: str,
    trace_id: str | None = None,
) -> None:
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
            trace_id=trace_id,
        )


def reply_subject(inbound_subject: str, *, fallback: str) -> str:
    """Return a reply subject, using fallback when the inbound subject is empty."""
    subject = inbound_subject.strip()
    if not subject:
        return fallback
    return f"Re: {subject}"


def _thread_headers(
    inbound_message_id: str | None,
    inbound_references: str | None = None,
) -> dict[str, str]:
    inbound_message_id = clean_message_id(inbound_message_id)
    if not inbound_message_id:
        return {}
    inbound_references = clean_references(inbound_references)
    references = (
        f"{inbound_references} {inbound_message_id}"
        if inbound_references
        else inbound_message_id
    )
    return {"in_reply_to": inbound_message_id, "references": references}


def _direct_reply_kwargs(
    inbound_message_id: str | None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    inbound_references: str | None = None,
) -> dict[str, str | None]:
    if not inbound_message_id:
        return {}
    kwargs: dict[str, str | None] = _thread_headers(
        inbound_message_id,
        inbound_references,
    )
    if not kwargs:
        return {}
    if inbound_body_for_quote:
        kwargs["quoted_body_text"] = inbound_body_for_quote
        kwargs["quoted_date"] = inbound_date
    return kwargs


def send_reply(
    to_address: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    quoted_body_text: str | None = None,
    quoted_date: str | None = None,
    include_footer: bool = True,
    trace_id: str | None = None,
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
    with audit_trace(trace_id), audit_span(
        "email.smtp_send",
        recipient_id_present=bool(to_address),
        subject_chars=len(subject),
        body_chars=len(body_text),
        html_present=body_html is not None,
    ):
        s = get_settings()

        if quoted_body_text:
            body_text = body_text + \
                _quoted_trail_text(quoted_body_text, quoted_date)
            if body_html:
                body_html = body_html + \
                    _quoted_trail_html(quoted_body_text, quoted_date)

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
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
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

        _append_to_sent(msg, trace_id=trace_id)
