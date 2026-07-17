"""MIME checks for safe, semantically equivalent user-facing HTML email."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from html import escape
import re
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
_QUOTED_TRAIL_RE = re.compile(
    r"(?s)(?P<prefix>(?:^|\n)On [^\n]+, you wrote:\n)"
    r"(?P<quoted>(?:[ \t]*>[^\n]*(?:\n|$))+)$"
)
_QUOTE_MARKER_RE = re.compile(r"(?m)^[ \t]*>[ \t]?")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_HIDDEN_RE = re.compile(
    r"(?:display\s*:\s*none\b|visibility\s*:\s*hidden\b|"
    r"(?:max-)?height\s*:\s*0(?:[a-z%]+)?\b|width\s*:\s*0(?:[a-z%]+)?\b|"
    r"opacity\s*:\s*0(?:\.0+)?\b|font-size\s*:\s*0(?:[a-z%]+)?\b|"
    r"line-height\s*:\s*0(?:[a-z%]+)?\b|mso-hide\s*:\s*all\b|"
    r"text-indent\s*:\s*-[0-9]+(?:px|em|rem|%)\b|"
    r"clip(?:-path)?\s*:)",
    re.IGNORECASE,
)
_CSS_UNSAFE_RULES = (
    (re.compile(r"@import\b", re.IGNORECASE), "remote stylesheet import"),
    (re.compile(r"url\s*\(", re.IGNORECASE), "remote resource in CSS"),
    (re.compile(r"expression\s*\(", re.IGNORECASE), "active CSS expression"),
    (re.compile(r"(?:-moz-binding|behavior)\s*:", re.IGNORECASE), "active CSS binding"),
    (re.compile(r"javascript\s*:", re.IGNORECASE), "active CSS URL"),
    (re.compile(r"\bcontent\s*:", re.IGNORECASE), "CSS generated content"),
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
    """Inspect a user-facing email against the presentation contract.

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
        plain_semantic_text = _normalize_semantic_text(
            plain_text,
            normalize_quoted_trail=_has_server_rendered_quoted_trail(html),
        )
        if plain_semantic_text != _normalize_semantic_text(visible_html_text):
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
    """Assert the message satisfies MIME, parity, and safety rules."""
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
        if name == "style":
            if tag.find_parent("head") is None:
                violations.append("style element outside head")
            violations.extend(_css_safety_violations(tag.get_text()))
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
            if lowered == "style":
                violations.extend(_css_safety_violations(rendered_value))

    for value in untrusted_values:
        if value and value in html and escape(value) != value:
            violations.append("unescaped fixture input appears in HTML")
    return violations


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _normalize_semantic_text(text: str, *, normalize_quoted_trail: bool = False) -> str:
    """Normalize signature syntax and an explicitly rendered trailing quote."""
    text = text.replace("\n--\n", "\n")
    if normalize_quoted_trail:
        text = _normalize_quoted_trail(text)
    return _normalize(text)


def _has_server_rendered_quoted_trail(html: str) -> bool:
    """Identify the final ``On …, you wrote:`` plus blockquote template shape."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    blockquotes = soup.find_all("blockquote")
    if not blockquotes:
        return False
    quote = blockquotes[-1]
    if quote.find_next_sibling(lambda node: getattr(node, "name", None)) is not None:
        return False
    prefix = quote.find_previous_sibling("p")
    return prefix is not None and bool(
        re.fullmatch(r"On .+, you wrote:", prefix.get_text(" ", strip=True))
    )


def _normalize_quoted_trail(text: str) -> str:
    """Remove plain quote markers only from the server's terminal quote trail."""
    match = _QUOTED_TRAIL_RE.search(text)
    if match is None:
        return text
    return text[: match.start("quoted")] + _QUOTE_MARKER_RE.sub(
        "", match.group("quoted")
    )


def _css_safety_violations(css: str) -> list[str]:
    """Permit static presentation CSS but reject active, remote, or hidden content."""
    uncommented = _CSS_COMMENT_RE.sub("", css)
    violations = [
        reason for pattern, reason in _CSS_UNSAFE_RULES if pattern.search(uncommented)
    ]
    if _CSS_HIDDEN_RE.search(uncommented) or any(
        marker in uncommented.lower() for marker in _HIDDEN_STYLE_MARKERS
    ):
        violations.append("hidden content style")
    return violations
