"""Trusted, server-owned rendering for user-facing email alternatives.

This module is the sole HTML rendering boundary. It accepts only canonical
plain text or a closed set of typed, server-selected message contexts. The
templates contain all markup and CSS; untrusted strings are always rendered by
Jinja with HTML autoescaping enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jinja2 import (
    Environment,
    PackageLoader,
    StrictUndefined,
    TemplateError,
    select_autoescape,
)

from thenetwork.settings import get_settings


class SignatureVariant(str, Enum):
    """Server-owned signature variants for user-facing mail."""

    STANDARD = "standard"
    STANDARD_WITH_REFERRAL = "standard_with_referral"
    NONE = "none"


class FixedEmailTemplate(str, Enum):
    """Closed set of fixed server-selected email bodies."""

    CONSENT_REQUEST = "consent_request"
    CONSENT_CLARIFICATION = "consent_clarification"
    CONSENT_ACKNOWLEDGMENT = "consent_acknowledgment"
    CONSENT_DECLINED = "consent_declined"
    CONSENT_ALREADY_DECLINED = "consent_already_declined"
    INTRODUCTION = "introduction"
    FIRST_CONTACT_WELCOME = "first_contact_welcome"
    INFRASTRUCTURE_REJECTION = "infrastructure_rejection"
    EVENT_RECOMMENDATION = "event_recommendation"


class InfrastructureRejectionReason(str, Enum):
    """Server-owned reasons for a fixed infrastructure rejection reply."""

    BODY_OVERSIZE = "body_oversize"
    RATE_LIMIT = "rate_limit"
    CONTENT_SCAN = "content_scan"


@dataclass(frozen=True, slots=True)
class IntroductionEmailContext:
    """Typed context for the post-consent introduction email."""

    relay_address: str
    person_a_gist: str | None = None
    person_b_gist: str | None = None


class EventRecommendationNotice(str, Enum):
    """Fixed capability instructions for an event recommendation."""

    FIRST = (
        "Would you like occasional event recommendations like this? Reply yes or no. "
        "A no stops only event recommendations."
    )
    STOP = 'To stop event recommendations, reply "stop event recommendations."'


@dataclass(frozen=True, slots=True)
class EventRecommendationEmailContext:
    """Typed sealed context for a one-way event recommendation."""

    event_gist: str
    notice: EventRecommendationNotice


@dataclass(frozen=True, slots=True)
class FirstContactWelcomeEmailContext:
    """Typed context for the fixed first-contact welcome email."""


@dataclass(frozen=True, slots=True)
class InfrastructureRejectionEmailContext:
    """Typed context for a fixed infrastructure rejection email."""

    reason: InfrastructureRejectionReason


@dataclass(frozen=True, slots=True)
class ConsentRequestEmailContext:
    """Typed, anonymous context for an introduction consent request."""

    counterpart_gist: str
    reply_token: str


@dataclass(frozen=True, slots=True)
class EmptyEmailContext:
    """Typed context for a fixed body with no interpolated fields."""


type FixedEmailContext = (
    IntroductionEmailContext
    | FirstContactWelcomeEmailContext
    | InfrastructureRejectionEmailContext
    | ConsentRequestEmailContext
    | EmptyEmailContext
    | EventRecommendationEmailContext
)


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    """Canonical inbound text quoted after the signature in a direct reply."""

    body_text: str
    date: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """Complete canonical plain text and its optional HTML alternative."""

    text: str
    html: str | None


# One process-wide environment. Only package templates can be loaded, all
# undefined fields fail loudly, and HTML templates autoescape every value.
_ENVIRONMENT = Environment(
    loader=PackageLoader("thenetwork.email", "templates"),
    undefined=StrictUndefined,
    autoescape=select_autoescape(enabled_extensions=("html",)),
)

_FIXED_TEMPLATE_FILES: dict[FixedEmailTemplate, tuple[str, str]] = {
    FixedEmailTemplate.CONSENT_REQUEST: (
        "fixed/consent_request.txt",
        "fixed/consent_request.html",
    ),
    FixedEmailTemplate.CONSENT_CLARIFICATION: (
        "fixed/consent_clarification.txt",
        "fixed/consent_clarification.html",
    ),
    FixedEmailTemplate.CONSENT_ACKNOWLEDGMENT: (
        "fixed/consent_acknowledgment.txt",
        "fixed/consent_acknowledgment.html",
    ),
    FixedEmailTemplate.CONSENT_DECLINED: (
        "fixed/consent_declined.txt",
        "fixed/consent_declined.html",
    ),
    FixedEmailTemplate.CONSENT_ALREADY_DECLINED: (
        "fixed/consent_already_declined.txt",
        "fixed/consent_already_declined.html",
    ),
    FixedEmailTemplate.INTRODUCTION: (
        "fixed/introduction.txt",
        "fixed/introduction.html",
    ),
    FixedEmailTemplate.FIRST_CONTACT_WELCOME: (
        "fixed/first_contact_welcome.txt",
        "fixed/first_contact_welcome.html",
    ),
    FixedEmailTemplate.INFRASTRUCTURE_REJECTION: (
        "fixed/infrastructure_rejection.txt",
        "fixed/infrastructure_rejection.html",
    ),
    FixedEmailTemplate.EVENT_RECOMMENDATION: (
        "fixed/event_recommendation.txt",
        "fixed/event_recommendation.html",
    ),
}

_INFRASTRUCTURE_REJECTION_COPY = {
    InfrastructureRejectionReason.BODY_OVERSIZE: (
        "We could not process your email because the message body was too large. "
        "Please send a shorter message and try again."
    ),
    InfrastructureRejectionReason.RATE_LIMIT: (
        "We could not process your email because this address is sending too many "
        "messages right now. Please wait and try again later."
    ),
    InfrastructureRejectionReason.CONTENT_SCAN: (
        "We could not process your email because it was blocked by an automated "
        "safety scan. Please revise the message and try again."
    ),
}

_STANDARD_SIGNATURE_TEXT = (
    "--\nThe Network\nAn automated connection service\nReply anytime."
)
_REFERRAL_TEXT = (
    "Know someone who should be on this? Forward this along — they can join by "
    "emailing {account} directly."
)


def render_conversational_email(
    body_text: str,
    *,
    signature_variant: SignatureVariant = SignatureVariant.STANDARD,
    quoted_message: QuotedMessage | None = None,
    referral_account: str | None = None,
) -> RenderedEmail:
    """Render canonical conversational text and its trusted HTML peer.

    Paragraphs are separated by blank lines and individual source line breaks
    become ``<br>`` elements. The input is never interpreted as HTML, Markdown,
    or URLs.
    """
    _require_text(body_text, "body_text")
    if quoted_message is not None:
        _require_text(quoted_message.body_text, "quoted_message.body_text")

    plain_body = _normalize_text(body_text)
    text = _assemble_plain_text(
        plain_body,
        signature_variant=signature_variant,
        quoted_message=quoted_message,
        referral_account=referral_account,
    )
    return _render_html_alternative(
        text,
        body_text=plain_body,
        signature_variant=signature_variant,
        quoted_message=quoted_message,
        referral_account=referral_account,
    )


def render_fixed_email(
    template: FixedEmailTemplate,
    context: FixedEmailContext,
    *,
    signature_variant: SignatureVariant = SignatureVariant.STANDARD,
    quoted_message: QuotedMessage | None = None,
    referral_account: str | None = None,
) -> RenderedEmail:
    """Render one named fixed template from its matching typed context.

    ``template`` selects a literal entry in an internal allowlist. It is never
    used as a file path or passed through from a caller as an arbitrary string.
    """
    if not isinstance(template, FixedEmailTemplate):
        raise TypeError("template must be a FixedEmailTemplate")
    template_context = _fixed_template_context(template, context)

    text_name, _html_name = _FIXED_TEMPLATE_FILES[template]
    plain_body = _ENVIRONMENT.get_template(text_name).render(template_context).strip()
    text = _assemble_plain_text(
        plain_body,
        signature_variant=signature_variant,
        quoted_message=quoted_message,
        referral_account=referral_account,
    )

    try:
        html = _render_document(
            body_paragraphs=None,
            fixed_template=template,
            fixed_context=template_context,
            signature_variant=signature_variant,
            quoted_message=quoted_message,
            referral_account=referral_account,
        )
    except TemplateError:
        return RenderedEmail(text=text, html=None)
    return RenderedEmail(text=text, html=html)


def _fixed_template_context(
    template: FixedEmailTemplate,
    context: FixedEmailContext,
) -> dict[str, str | None]:
    if template is FixedEmailTemplate.INTRODUCTION:
        if not isinstance(context, IntroductionEmailContext):
            raise TypeError("introduction requires IntroductionEmailContext")
        _require_text(context.relay_address, "context.relay_address")
        if (context.person_a_gist is None) != (context.person_b_gist is None):
            raise ValueError("introduction match recap requires both participant gists")
        if context.person_a_gist is not None:
            _require_text(context.person_a_gist, "context.person_a_gist")
            _require_text(context.person_b_gist, "context.person_b_gist")
        return {
            "relay_address": context.relay_address,
            "person_a_gist": context.person_a_gist,
            "person_b_gist": context.person_b_gist,
        }
    if template is FixedEmailTemplate.FIRST_CONTACT_WELCOME:
        if not isinstance(context, FirstContactWelcomeEmailContext):
            raise TypeError(
                "first_contact_welcome requires FirstContactWelcomeEmailContext"
            )
        return {}
    if template is FixedEmailTemplate.INFRASTRUCTURE_REJECTION:
        if not isinstance(context, InfrastructureRejectionEmailContext):
            raise TypeError(
                "infrastructure_rejection requires InfrastructureRejectionEmailContext"
            )
        if not isinstance(context.reason, InfrastructureRejectionReason):
            raise TypeError("context.reason must be an InfrastructureRejectionReason")
        return {"message": _INFRASTRUCTURE_REJECTION_COPY[context.reason]}
    if template is FixedEmailTemplate.EVENT_RECOMMENDATION:
        if not isinstance(context, EventRecommendationEmailContext):
            raise TypeError(
                "event recommendation requires EventRecommendationEmailContext"
            )
        _require_text(context.event_gist, "context.event_gist")
        if not isinstance(context.notice, EventRecommendationNotice):
            raise TypeError("context.notice must be an EventRecommendationNotice")
        return {
            "event_gist": context.event_gist,
            "notice": context.notice.value,
        }
    if template is FixedEmailTemplate.CONSENT_REQUEST:
        if not isinstance(context, ConsentRequestEmailContext):
            raise TypeError("consent request requires ConsentRequestEmailContext")
        _require_text(context.counterpart_gist, "context.counterpart_gist")
        _require_text(context.reply_token, "context.reply_token")
        return {
            "counterpart_gist": context.counterpart_gist,
            "reply_token": context.reply_token,
        }
    if template in {
        FixedEmailTemplate.CONSENT_CLARIFICATION,
        FixedEmailTemplate.CONSENT_ACKNOWLEDGMENT,
        FixedEmailTemplate.CONSENT_DECLINED,
        FixedEmailTemplate.CONSENT_ALREADY_DECLINED,
    }:
        if not isinstance(context, EmptyEmailContext):
            raise TypeError(f"{template.value} requires EmptyEmailContext")
        return {}
    raise AssertionError(f"unhandled fixed template: {template}")


def _render_html_alternative(
    text: str,
    *,
    body_text: str,
    signature_variant: SignatureVariant,
    quoted_message: QuotedMessage | None,
    referral_account: str | None,
) -> RenderedEmail:
    try:
        html = _render_document(
            body_paragraphs=_paragraphs(body_text),
            fixed_template=None,
            fixed_context=None,
            signature_variant=signature_variant,
            quoted_message=quoted_message,
            referral_account=referral_account,
        )
    except TemplateError:
        return RenderedEmail(text=text, html=None)
    return RenderedEmail(text=text, html=html)


def _render_document(
    *,
    body_paragraphs: tuple[tuple[str, ...], ...] | None,
    fixed_template: FixedEmailTemplate | None,
    fixed_context: dict[str, str] | None,
    signature_variant: SignatureVariant,
    quoted_message: QuotedMessage | None,
    referral_account: str | None,
) -> str:
    signature = _signature_context(signature_variant, referral_account)
    quote = _quote_context(quoted_message)
    return _ENVIRONMENT.get_template("email.html").render(
        body_paragraphs=body_paragraphs,
        fixed_template=fixed_template.value if fixed_template is not None else None,
        fixed_context=fixed_context,
        signature=signature,
        quote=quote,
    )


def _assemble_plain_text(
    body: str,
    *,
    signature_variant: SignatureVariant,
    quoted_message: QuotedMessage | None,
    referral_account: str | None,
) -> str:
    text = body
    signature = _signature_context(signature_variant, referral_account)
    if signature["plain"]:
        text = f"{text}\n\n{signature['plain']}"
    if quoted_message is not None:
        date = quoted_message.date or "an earlier message"
        quote_lines = [
            f"On {date}, you wrote:",
            *(
                f"> {line}" if line else ">"
                for line in _normalize_text(quoted_message.body_text).split("\n")
            ),
        ]
        text = f"{text}\n\n{'\n'.join(quote_lines)}"
    return text


def _signature_context(
    variant: SignatureVariant, referral_account: str | None
) -> dict[str, str | bool]:
    if not isinstance(variant, SignatureVariant):
        raise TypeError("signature_variant must be a SignatureVariant")
    if variant is SignatureVariant.NONE:
        return {"plain": "", "show": False, "referral": ""}
    referral = ""
    if variant is SignatureVariant.STANDARD_WITH_REFERRAL:
        account = (
            referral_account
            if referral_account is not None
            else get_settings().imap_account
        )
        _require_text(account, "referral_account")
        referral = _REFERRAL_TEXT.format(account=account)
    plain = _STANDARD_SIGNATURE_TEXT
    if referral:
        plain = f"{plain}\n{referral}"
    return {"plain": plain, "show": True, "referral": referral}


def _quote_context(
    quoted_message: QuotedMessage | None,
) -> dict[str, str | tuple[str, ...]] | None:
    if quoted_message is None:
        return None
    return {
        "date": quoted_message.date or "an earlier message",
        "lines": tuple(_normalize_text(quoted_message.body_text).split("\n")),
    }


def _paragraphs(text: str) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(paragraph.split("\n")) for paragraph in text.split("\n\n") if paragraph
    )


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
