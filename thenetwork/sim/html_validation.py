"""Synthetic MIME checks for the HTML-email rollout.

These helpers deliberately inspect messages built by tests and client fixtures,
not production templates.  They make the rendering contract executable without
turning simulation artifacts into HTML previews.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from html import escape
from typing import Iterable

from bs4 import BeautifulSoup


_FORBIDDEN_ELEMENTS = frozenset(
    {
        "audio",
        "base",
        "button",
        "canvas",
        "embed",
        "form",
        "iframe",
        "img",
        "input",
        "link",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "textarea",
        "video",
    }
)
_RESOURCE_ATTRIBUTES = frozenset({"action", "background", "href", "poster", "src"})
_HIDDEN_STYLE_MARKERS = (
    "display:none",
    "display: none",
    "visibility:hidden",
    "visibility: hidden",
    "max-height:0",
    "max-height: 0",
    "mso-hide:all",
    "mso-hide: all",
)


@dataclass(frozen=True)
class HtmlEmailInspection:
    """The safe, fixture-level facts needed to validate a rendered email."""

    plain_text: str | None
    html: str | None
    visible_html_text: str | None
    part_types: tuple[str, ...]
    violations: tuple[str, ...]


def inspect_html_email(
    message: Message,
    *,
    required_text: Iterable[str] = (),
    untrusted_values: Iterable[str] = (),
) -> HtmlEmailInspection:
    """Inspect a synthetic user-facing email against the presentation contract.

    ``required_text`` is useful for signatures and capability tokens that must
    appear in both alternatives. ``untrusted_values`` detects raw interpolation
    only when a fixture intentionally includes the source value in its HTML.
    """
    violations: list[str] = []
    parts = tuple(message.iter_parts()) if message.is_multipart() else ()
    part_types = tuple(part.get_content_type() for part in parts)
    if message.get_content_type() != "multipart/alternative":
        violations.append("message is not multipart/alternative")
    if part_types != ("text/plain", "text/html"):
        violations.append("alternatives must be text/plain followed by text/html")

    plain_text = _part_content(parts[0]) if len(parts) >= 1 else None
    html = _part_content(parts[-1]) if len(parts) >= 2 else None
    visible_html_text = html_to_visible_text(html) if html is not None else None

    if plain_text is not None and visible_html_text is not None:
        if _normalize_semantic_text(plain_text) != _normalize_semantic_text(
            visible_html_text
        ):
            violations.append("plain text and visible HTML text differ")
    for text in required_text:
        normalized = _normalize(text)
        if normalized and (
            plain_text is None or normalized not in _normalize(plain_text)
        ):
            violations.append(f"required text missing from plain part: {text!r}")
        if normalized and (
            visible_html_text is None or normalized not in _normalize(visible_html_text)
        ):
            violations.append(f"required text missing from HTML part: {text!r}")

    if html is not None:
        violations.extend(_html_safety_violations(html, untrusted_values))

    return HtmlEmailInspection(
        plain_text=plain_text,
        html=html,
        visible_html_text=visible_html_text,
        part_types=part_types,
        violations=tuple(violations),
    )


def assert_html_email_contract(
    message: Message,
    *,
    required_text: Iterable[str] = (),
    untrusted_values: Iterable[str] = (),
) -> HtmlEmailInspection:
    """Assert the synthetic message satisfies MIME, parity, and safety rules."""
    inspection = inspect_html_email(
        message,
        required_text=required_text,
        untrusted_values=untrusted_values,
    )
    if inspection.violations:
        raise AssertionError(
            "HTML email contract violations: " + "; ".join(inspection.violations)
        )
    return inspection


def html_to_visible_text(html: str) -> str:
    """Return only visible semantic text; never use HTML itself as prompt input."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    for tag in soup(("head", "script", "style", "template", "title")):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _part_content(part: Message) -> str:
    try:
        content = part.get_content()
    except Exception:
        return ""
    return content if isinstance(content, str) else ""


def _html_safety_violations(
    html: str,
    untrusted_values: Iterable[str],
) -> list[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ["HTML could not be parsed"]

    violations: list[str] = []
    for tag in soup.find_all(True):
        name = tag.name.lower()
        if name in _FORBIDDEN_ELEMENTS:
            violations.append(f"forbidden HTML element: {name}")
        for attribute, value in tag.attrs.items():
            lowered = attribute.lower()
            rendered_value = " ".join(value) if isinstance(value, list) else str(value)
            if lowered.startswith("on"):
                violations.append(f"event handler attribute: {attribute}")
            if lowered in _RESOURCE_ATTRIBUTES:
                violations.append(f"resource or navigation attribute: {attribute}")
            if lowered == "hidden" or (
                lowered == "aria-hidden" and rendered_value.lower() == "true"
            ):
                violations.append(f"hidden content attribute: {attribute}")
            if lowered == "style" and any(
                marker in rendered_value.lower() for marker in _HIDDEN_STYLE_MARKERS
            ):
                violations.append("hidden content style")

    for value in untrusted_values:
        if value and value in html and escape(value) != value:
            violations.append("unescaped fixture input appears in HTML")
    return violations


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _normalize_semantic_text(text: str) -> str:
    """Treat the plain-text signature delimiter and HTML divider as equivalent."""
    return _normalize(text.replace("\n--\n", "\n"))
