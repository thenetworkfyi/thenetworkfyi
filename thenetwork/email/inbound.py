"""IMAP inbox polling via imap-tools."""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Iterator

from imap_tools import AND, MailBox, MailMessageFlags

from thenetwork.settings import get_settings


MAX_SUBJECT_CHARS = 300
MAX_BODY_CHARS = 10_000
MAX_RAW_BODY_CHARS = 100_000
MIN_BODY_TEXT_CHARS = 3
REJECT_BODY_EMPTY = "body_empty"
REJECT_BODY_OVERSIZE = "body_oversize"

_AUTH_RESULT_RE = re.compile(r"\b(dkim|spf)=(\w+)", re.IGNORECASE)
_AUTHSERV_ID_RE = re.compile(r"^\s*([^;]+)")

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
    # True if the receiving mail server's own Authentication-Results header
    # reports dkim=pass or spf=pass for this message. The From: header alone
    # is spoofable (imap-tools has no access to the SMTP envelope), so
    # callers must not trust `sender` for identity resolution unless this
    # is True.
    sender_authenticated: bool
    rejection_reason: str | None = None
    body_chars: int | None = None


class BodyTooLargeError(ValueError):
    """Raised when decoded body text crosses the hard inbound reject limit."""

    def __init__(self, body_chars: int) -> None:
        super().__init__("decoded inbound body exceeds hard limit")
        self.body_chars = body_chars


def cap_subject(subject: str | None) -> str:
    """Return a subject bounded for audit, queues, and model context."""
    return (subject or "")[:MAX_SUBJECT_CHARS]


def is_near_empty_body(body: str) -> bool:
    """Return True for body text too small to spend an agent run on."""
    return len(body.strip()) < MIN_BODY_TEXT_CHARS


def cap_body(body: str) -> str:
    """Return body text bounded for downstream scanners and model context."""
    if len(body) > MAX_RAW_BODY_CHARS:
        raise BodyTooLargeError(len(body))
    return body[:MAX_BODY_CHARS]


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
    return cap_body(body)


def _is_sender_authenticated(msg) -> bool:
    """True if the receiving server vouches for this message's DKIM/SPF.

    Trusts only the Authentication-Results header nearest the top of the
    message — the one added last, by our own receiving MTA — since every
    intermediate hop pushes prior (potentially attacker-forged) copies of
    this header further down. If ``trusted_authserv_id`` is configured, that
    header's authserv-id must also match, guarding against an untrusted
    relay in between.
    """
    s = get_settings()
    if not s.require_sender_auth:
        return True

    values = msg.headers.get("authentication-results")
    if not values:
        return False

    header_value = values[0]
    if s.trusted_authserv_id:
        m = _AUTHSERV_ID_RE.match(header_value)
        authserv_id = m.group(1).strip() if m else ""
        if authserv_id.lower() != s.trusted_authserv_id.lower():
            return False

    verdicts = {mech.lower(): result.lower() for mech, result in _AUTH_RESULT_RE.findall(header_value)}
    return verdicts.get("dkim") == "pass" or verdicts.get("spf") == "pass"


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
            subject = cap_subject(msg.subject)
            try:
                body = extract_body(msg.obj)
            except BodyTooLargeError as exc:
                messages.append(
                    InboundMessage(
                        uid=msg.uid,
                        sender=msg.from_,
                        subject=subject,
                        body="",
                        auto_submitted=auto_sub[0] if auto_sub else None,
                        sender_authenticated=_is_sender_authenticated(msg),
                        rejection_reason=REJECT_BODY_OVERSIZE,
                        body_chars=exc.body_chars,
                    )
                )
                continue
            rejection_reason = REJECT_BODY_EMPTY if is_near_empty_body(body) else None
            messages.append(
                InboundMessage(
                    uid=msg.uid,
                    sender=msg.from_,
                    subject=subject,
                    body=body,
                    auto_submitted=auto_sub[0] if auto_sub else None,
                    sender_authenticated=_is_sender_authenticated(msg),
                    rejection_reason=rejection_reason,
                    body_chars=len(body),
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
