"""IMAP inbox polling via imap-tools."""
from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Iterator

from imap_tools import AND, MailBox, MailMessageFlags

from thenetwork.settings import get_settings


MAX_BODY_CHARS = 50_000

_HTML_HIDDEN_ELEMENTS = frozenset({"head", "script", "style", "template", "title"})
_HTML_BREAK_ELEMENTS = frozenset(
    {"br", "div", "hr", "li", "ol", "p", "table", "td", "th", "tr", "ul"}
)


@dataclass
class InboundMessage:
    uid: str
    sender: str
    subject: str
    body: str
    # RFC 3834 loop prevention headers, if present
    auto_submitted: str | None


class _VisibleTextParser(HTMLParser):
    """Extract visible text from an HTML email body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _HTML_HIDDEN_ELEMENTS:
            self._hidden_depth += 1
        elif not self._hidden_depth and tag in _HTML_BREAK_ELEMENTS:
            self._parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if not self._hidden_depth and tag.lower() in _HTML_BREAK_ELEMENTS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _HTML_HIDDEN_ELEMENTS and self._hidden_depth:
            self._hidden_depth -= 1
        elif not self._hidden_depth and tag in _HTML_BREAK_ELEMENTS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


def _html_to_text(html: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    return parser.text()


def _is_attachment(part: Message) -> bool:
    """Identify attachment containers as well as attachment leaf parts."""
    return (
        part.get_content_disposition() == "attachment"
        or part.get_filename() is not None
        or part.get("Content-ID") is not None
    )


def _iter_body_parts(part: Message) -> Iterator[Message]:
    """Yield body candidates while pruning complete attachment subtrees."""
    if _is_attachment(part) or part.get_content_type() == "message/rfc822":
        return

    if part.is_multipart():
        payload = part.get_payload()
        if isinstance(payload, list):
            for child in payload:
                yield from _iter_body_parts(child)
        return

    if part.get_content_type() in {"text/plain", "text/html"}:
        yield part


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        undecoded = part.get_payload()
        return undecoded if isinstance(undecoded, str) else ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_body(message: Message) -> str:
    """Return plain body text without reading or descending into attachments."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in _iter_body_parts(message):
        text = _decode_part(part)
        if part.get_content_type() == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(text)

    body = "".join(plain_parts) if plain_parts else _html_to_text("".join(html_parts))
    return body[:MAX_BODY_CHARS]


def _is_auto_message(msg) -> bool:
    """Return True if inbound mail should be skipped per RFC 3834 loop prevention."""
    auto = msg.headers.get("auto-submitted")
    if auto and auto[0].lower() != "no":
        return True
    precedence = msg.headers.get("precedence")
    if precedence and precedence[0].lower() in ("bulk", "list", "junk"):
        return True
    # RFC 2369 mailing list headers — any of these indicates a list message
    for list_header in ("list-id", "list-unsubscribe", "list-post", "list-subscribe"):
        if msg.headers.get(list_header):
            return True
    return False


def poll_unseen() -> list[InboundMessage]:
    """Fetch unseen messages WITHOUT marking them seen.

    Caller is responsible for calling mark_messages_seen() after successfully
    enqueuing each message — this ensures no email is lost if the process
    crashes between fetch and enqueue (RFC 3834 durable intake).
    Skips auto-generated messages and self-sends to prevent mail loops.
    """
    s = get_settings()
    messages: list[InboundMessage] = []
    own_address = s.email_account.lower()

    with MailBox(s.imap_host, s.imap_port).login(s.email_account, s.email_password) as mb:
        for msg in mb.fetch(AND(seen=False), mark_seen=False, bulk=True):
            if _is_auto_message(msg):
                continue
            # Skip our own outbound replies that bounce back via IMAP
            if msg.from_.lower() == own_address:
                continue
            auto_sub = msg.headers.get("auto-submitted")
            messages.append(
                InboundMessage(
                    uid=msg.uid,
                    sender=msg.from_,
                    subject=msg.subject,
                    body=extract_body(msg.obj),
                    auto_submitted=auto_sub[0] if auto_sub else None,
                )
            )
    return messages


def mark_messages_seen(uids: list[str]) -> None:
    """Mark specific message UIDs as seen. Call after successful job enqueue."""
    if not uids:
        return
    s = get_settings()
    with MailBox(s.imap_host, s.imap_port).login(s.email_account, s.email_password) as mb:
        mb.flag(uids, [MailMessageFlags.SEEN], True)
