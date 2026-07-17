"""SMTP outbound using stdlib email.message.EmailMessage + smtplib over STARTTLS."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from time import monotonic

from imap_tools import MailBox, MailMessageFlags

from thenetwork.audit import audit_event, audit_span, audit_trace
from thenetwork.email.render import (
    EventRecommendationEmailContext,
    EventRecommendationNotice,
    FirstContactWelcomeEmailContext,
    FixedEmailContext,
    FixedEmailTemplate,
    IntroductionEmailContext,
    QuotedMessage,
    SignatureVariant,
    render_conversational_email,
    render_fixed_email,
)
from thenetwork.email.threading import clean_message_id, clean_references
from thenetwork.settings import get_settings

# The one deterministic explanation of what this address is and how to use
# it - injection-proof by construction since it's fixed copy, not model
# output. Sent both for near-empty first contact (worker/tasks.py) and when
# escalate() routes an authenticated first contact to a standard welcome
# instead of human escalation (agent/tools.py) - in both cases the sender
# doesn't yet know how to join.
FIRST_CONTACT_WELCOME_REPLY = render_fixed_email(
    FixedEmailTemplate.FIRST_CONTACT_WELCOME,
    FirstContactWelcomeEmailContext(),
    signature_variant=SignatureVariant.NONE,
    html_enabled=False,
).text

MAX_QUOTED_TRAIL_CHARS = 2_000
EVENT_RECOMMENDATION_SUBJECT = "An event you might care about"


def _quoted_body_lines(body_text: str) -> tuple[list[str], bool]:
    body = body_text.replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith(">")
    )
    truncated = len(body) > MAX_QUOTED_TRAIL_CHARS
    if truncated:
        body = body[:MAX_QUOTED_TRAIL_CHARS].rstrip()
    return body.splitlines(), truncated


def _quoted_message(body_text: str, quoted_date: str | None = None) -> QuotedMessage:
    """Return the bounded, de-nested inbound text for the trusted renderer."""
    body_lines, truncated = _quoted_body_lines(body_text)
    if truncated:
        body_lines.append("[quoted text truncated]")
    return QuotedMessage(body_text="\n".join(body_lines), date=quoted_date)


def _user_facing_signature_variant(
    settings, *, include_footer: bool = True
) -> SignatureVariant:
    """Choose the server-owned signature; only configured mail gets referral copy."""
    if not include_footer:
        return SignatureVariant.NONE
    if settings.growth_footer_enabled:
        return SignatureVariant.STANDARD_WITH_REFERRAL
    return SignatureVariant.STANDARD


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
            with MailBox(s.imap_host, s.imap_port).login(
                s.imap_account, s.imap_password
            ) as mb:
                mb.append(
                    msg.as_bytes(), s.imap_sent_folder, flag_set=[MailMessageFlags.SEEN]
                )
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
    body_text: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    quoted_body_text: str | None = None,
    quoted_date: str | None = None,
    include_footer: bool = True,
    trace_id: str | None = None,
    fixed_template: FixedEmailTemplate | None = None,
    fixed_context: FixedEmailContext | None = None,
) -> None:
    """Send an email from the configured account.

    Uses Auto-Submitted: auto-replied header (RFC 3834) so recipients'
    IMAP pollers skip our outbound replies and don't create a loop.

    The trusted renderer, rather than any caller, owns the HTML alternative,
    signature, referral footer, and quoted-message markup. ``fixed_template``
    is an internal, closed server-selected path for fixed replies; it accepts
    only a named server-owned template with its matching typed context, never
    caller-authored markup.
    Set ``include_footer=False`` for internal/ops mail that should have no
    signature or referral copy.
    """
    if fixed_template is not None and fixed_context is None:
        raise TypeError("fixed_template requires fixed_context")
    if fixed_template is None and fixed_context is not None:
        raise TypeError("fixed_context requires fixed_template")
    if fixed_template is not None and body_text is not None:
        raise TypeError("fixed-template replies do not accept body_text")
    if fixed_template is None and not isinstance(body_text, str):
        raise TypeError("conversational replies require body_text")
    template_id = (
        fixed_template.value if fixed_template is not None else "conversational"
    )
    with (
        audit_trace(trace_id),
        audit_span(
            "email.smtp_send",
            recipient_count=1,
            template_id=template_id,
        ),
    ):
        s = get_settings()
        signature_variant = _user_facing_signature_variant(
            s, include_footer=include_footer
        )
        quoted_message = (
            _quoted_message(quoted_body_text, quoted_date) if quoted_body_text else None
        )
        if fixed_template is None:
            rendered = render_conversational_email(
                body_text,
                signature_variant=signature_variant,
                quoted_message=quoted_message,
                html_enabled=s.html_email_enabled,
                referral_account=s.imap_account,
            )
        else:
            rendered = render_fixed_email(
                fixed_template,
                fixed_context,
                signature_variant=signature_variant,
                quoted_message=quoted_message,
                html_enabled=s.html_email_enabled,
                referral_account=s.imap_account,
            )
        audit_event(
            "email.rendered",
            html_present=rendered.html is not None,
            template_id=template_id,
            recipient_count=1,
            outcome="success",
        )

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

        msg.set_content(rendered.text)
        if rendered.html is not None:
            msg.add_alternative(rendered.html, subtype="html")

        with smtplib.SMTP(s.smtp_host, s.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(s.smtp_account, s.smtp_password)
            smtp.send_message(msg)

        _append_to_sent(msg, trace_id=trace_id)


def send_event_fyi(
    *,
    to_address: str,
    event_gist: str,
    notice: EventRecommendationNotice,
    trace_id: str | None = None,
) -> None:
    """Send a fixed, sealed event recommendation to one resolved recipient.

    This is intentionally not a general-purpose email entry point: the subject,
    body structure, and opt-out wording are all server-owned. Callers can supply
    only the current sanitized event gist and one of the closed notice variants.
    """
    with (
        audit_trace(trace_id),
        audit_span(
            "email.smtp_send",
            recipient_count=1,
            template_id=FixedEmailTemplate.EVENT_RECOMMENDATION.value,
        ),
    ):
        settings = get_settings()
        rendered = render_fixed_email(
            FixedEmailTemplate.EVENT_RECOMMENDATION,
            EventRecommendationEmailContext(event_gist=event_gist, notice=notice),
            signature_variant=_user_facing_signature_variant(settings),
            html_enabled=settings.html_email_enabled,
            referral_account=settings.imap_account,
        )
        audit_event(
            "email.rendered",
            html_present=rendered.html is not None,
            template_id=FixedEmailTemplate.EVENT_RECOMMENDATION.value,
            recipient_count=1,
            outcome="success",
        )

        msg = EmailMessage()
        msg["From"] = settings.email_from
        msg["To"] = to_address
        msg["Subject"] = EVENT_RECOMMENDATION_SUBJECT
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg["Auto-Submitted"] = "auto-replied"
        msg.set_content(rendered.text)
        if rendered.html is not None:
            msg.add_alternative(rendered.html, subtype="html")

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_account, settings.smtp_password)
            smtp.send_message(msg)

        _append_to_sent(msg, trace_id=trace_id)


def send_group_introduction(
    *,
    person_a_name: str,
    person_a_email: str,
    person_b_name: str,
    person_b_email: str,
    trace_id: str | None = None,
) -> None:
    """Send the fixed identity-revealing email after server-verified consent."""
    with (
        audit_trace(trace_id),
        audit_span(
            "email.smtp_send",
            recipient_count=2,
            template_id=FixedEmailTemplate.INTRODUCTION.value,
        ),
    ):
        settings = get_settings()
        rendered = render_fixed_email(
            FixedEmailTemplate.INTRODUCTION,
            IntroductionEmailContext(
                person_a_name=person_a_name,
                person_b_name=person_b_name,
            ),
            signature_variant=SignatureVariant.NONE,
            html_enabled=settings.html_email_enabled,
        )
        audit_event(
            "email.rendered",
            html_present=rendered.html is not None,
            template_id=FixedEmailTemplate.INTRODUCTION.value,
            recipient_count=2,
            outcome="success",
        )
        msg = EmailMessage()
        msg["From"] = settings.email_from
        msg["To"] = (person_a_email, person_b_email)
        msg["Subject"] = "Your introduction"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg["Auto-Submitted"] = "auto-replied"
        msg.set_content(rendered.text)
        if rendered.html is not None:
            msg.add_alternative(rendered.html, subtype="html")

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_account, settings.smtp_password)
            smtp.send_message(msg)

        _append_to_sent(msg, trace_id=trace_id)
