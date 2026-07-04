"""IMAP inbox polling via imap-tools."""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from imap_tools import AND, MailBox, MailMessageFlags
from imap_tools.message import MailMessage

from thenetwork.settings import get_settings


MAX_SUBJECT_CHARS = 300
MAX_BODY_CHARS = 10_000
MAX_RAW_BODY_CHARS = 100_000
MIN_BODY_TEXT_CHARS = 3
REJECT_BODY_EMPTY = "body_empty"
REJECT_BODY_OVERSIZE = "body_oversize"

_AUTH_RESULT_RE = re.compile(r"\b(dkim|spf)=(\w+)", re.IGNORECASE)
_AUTHSERV_ID_RE = re.compile(r"^\s*([^;]+)")

_HTML_HIDDEN_ELEMENTS = ("head", "script", "style", "template", "title")


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
    # Original raw MIME bytes, exactly as received. Only captured for
    # messages whose subject looks like an admin request - PGP/MIME
    # signature verification (admin/auth.py) needs the byte-exact original
    # bytes of the signed part, which re-serializing the parsed
    # email.message.Message does not round-trip (CRLF normalizes to LF).
    raw_message: bytes | None = None


class _RawCapturingMailMessage(MailMessage):
    """MailMessage subclass that also retains the original raw bytes.

    imap-tools parses fetch data into `self.obj` via
    `email.message_from_bytes()` but discards the raw bytes afterward.
    `BaseMailBox.email_message_class` is a designed extension point for
    swapping in a subclass like this one, so no hand-rolled IMAP FETCH is
    needed to recover them.
    """

    def __init__(self, fetch_data: list) -> None:
        super().__init__(fetch_data)
        raw_message_data, _, _ = self._get_message_data_parts(fetch_data)
        self.raw_message_bytes: bytes = raw_message_data


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


def _html_to_text(html: str) -> str:
    """Reduce an HTML email body to whitespace-normalized visible text."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    for tag in soup(_HTML_HIDDEN_ELEMENTS):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _is_sender_authenticated(msg) -> bool:
    """True if the receiving server vouches for this message's DKIM/SPF.

    Trusts only the Authentication-Results header nearest the top of the
    message - the one added last, by our own receiving MTA - since every
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
    # RFC 2369 mailing list headers - any of these indicates a list message
    for list_header in ("list-id", "list-unsubscribe", "list-post", "list-subscribe"):
        if msg.headers.get(list_header):
            return True
    return False


def poll_unseen() -> list[InboundMessage]:
    """Fetch unseen messages WITHOUT marking them seen.

    Caller is responsible for calling mark_messages_seen() after successfully
    enqueuing each message - this ensures no email is lost if the process
    crashes between fetch and enqueue (RFC 3834 durable intake).
    Skips auto-generated messages and self-sends to prevent mail loops.
    """
    s = get_settings()
    messages: list[InboundMessage] = []
    # Outbound replies carry From: email_from, not the polled imap_account,
    # so that's the address to match to skip our own replies bouncing back.
    own_addresses = {s.imap_account.lower(), s.email_from.lower()}

    with MailBox(s.imap_host, s.imap_port).login(s.imap_account, s.imap_password) as mb:
        mb.email_message_class = _RawCapturingMailMessage
        for msg in mb.fetch(AND(seen=False), mark_seen=False, bulk=True):
            if _is_auto_message(msg):
                continue
            # Skip our own outbound replies that bounce back via IMAP
            if msg.from_.lower() in own_addresses:
                continue
            auto_sub = msg.headers.get("auto-submitted")
            subject = cap_subject(msg.subject)
            # Only admin-looking subjects need the raw bytes (PGP/MIME
            # verification in admin/auth.py); everything else discards them
            # to avoid holding the full raw message in memory.
            raw_message = msg.raw_message_bytes if subject.strip().lower().startswith("admin:") else None
            try:
                body = cap_body(msg.text or _html_to_text(msg.html))
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
                        raw_message=raw_message,
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
                    raw_message=raw_message,
                )
            )
    return messages


def mark_messages_seen(uids: list[str]) -> None:
    """Mark specific message UIDs as seen. Call after successful job enqueue."""
    if not uids:
        return
    s = get_settings()
    with MailBox(s.imap_host, s.imap_port).login(s.imap_account, s.imap_password) as mb:
        mb.flag(uids, [MailMessageFlags.SEEN], True)
