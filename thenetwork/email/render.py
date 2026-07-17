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

    INTRODUCTION = "introduction"


@dataclass(frozen=True, slots=True)
class IntroductionEmailContext:
    """Typed context for the post-consent introduction email."""

    person_a_name: str
    person_b_name: str


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
    FixedEmailTemplate.INTRODUCTION: (
        "fixed/introduction.txt",
        "fixed/introduction.html",
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
    html_enabled: bool | None = None,
    referral_account: str | None = None,
) -> RenderedEmail:
    """Render canonical conversational text and, when enabled, its HTML peer.

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
        html_enabled=html_enabled,
        referral_account=referral_account,
    )


def render_fixed_email(
    template: FixedEmailTemplate,
    context: IntroductionEmailContext,
    *,
    signature_variant: SignatureVariant = SignatureVariant.STANDARD,
    html_enabled: bool | None = None,
    referral_account: str | None = None,
) -> RenderedEmail:
    """Render one named fixed template from its matching typed context.

    ``template`` selects a literal entry in an internal allowlist. It is never
    used as a file path or passed through from a caller as an arbitrary string.
    """
    if not isinstance(template, FixedEmailTemplate):
        raise TypeError("template must be a FixedEmailTemplate")
    if template is not FixedEmailTemplate.INTRODUCTION or not isinstance(
        context, IntroductionEmailContext
    ):
        raise TypeError("introduction requires IntroductionEmailContext")
    _require_text(context.person_a_name, "context.person_a_name")
    _require_text(context.person_b_name, "context.person_b_name")

    text_name, _html_name = _FIXED_TEMPLATE_FILES[template]
    template_context = {
        "person_a_name": context.person_a_name,
        "person_b_name": context.person_b_name,
    }
    plain_body = _ENVIRONMENT.get_template(text_name).render(template_context).strip()
    text = _assemble_plain_text(
        plain_body,
        signature_variant=signature_variant,
        quoted_message=None,
        referral_account=referral_account,
    )

    if not _html_is_enabled(html_enabled):
        return RenderedEmail(text=text, html=None)
    try:
        html = _render_document(
            body_paragraphs=None,
            fixed_template=template,
            fixed_context=template_context,
            signature_variant=signature_variant,
            quoted_message=None,
            referral_account=referral_account,
        )
    except TemplateError:
        return RenderedEmail(text=text, html=None)
    return RenderedEmail(text=text, html=html)


def _render_html_alternative(
    text: str,
    *,
    body_text: str,
    signature_variant: SignatureVariant,
    quoted_message: QuotedMessage | None,
    html_enabled: bool | None,
    referral_account: str | None,
) -> RenderedEmail:
    if not _html_is_enabled(html_enabled):
        return RenderedEmail(text=text, html=None)
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


def _html_is_enabled(html_enabled: bool | None) -> bool:
    return get_settings().html_email_enabled if html_enabled is None else html_enabled


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
