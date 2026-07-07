"""IMAP inbox polling via imap-tools."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import uuid4

from bs4 import BeautifulSoup
from imap_tools import AND, MailBox, MailMessageFlags
from imap_tools.message import MailMessage

from thenetwork.audit import audit_warning_event
from thenetwork.email.threading import clean_message_id, clean_references
from thenetwork.settings import get_settings


MAX_SUBJECT_CHARS = 300
MAX_SENDER_NAME_CHARS = 300
MAX_BODY_CHARS = 10_000
MAX_RAW_BODY_CHARS = 100_000
MIN_BODY_TEXT_CHARS = 3
REJECT_BODY_EMPTY = "body_empty"
REJECT_BODY_OVERSIZE = "body_oversize"

_AUTH_RESULT_RE = re.compile(r"\b(dkim|spf|auth)=(\w+)", re.IGNORECASE)
_AUTH_MECHANISM_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.:-]{0,79})=", re.IGNORECASE)
_AUTHSERV_ID_RE = re.compile(r"^\s*([^;]+)")
_SAFE_AUTHSERV_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_WARNED_UNRECOGNIZED_AUTH_RESULTS: set[tuple[str, tuple[str, ...]]] = set()

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
    # reports dkim=pass, spf=pass, or auth=pass for this message. The From:
    # header alone is spoofable (imap-tools has no access to the SMTP
    # envelope), so callers must not trust `sender` for identity resolution
    # unless this is True.
    sender_authenticated: bool
    # From: header display name (e.g. "First Last" in "First Last
    # <first.last@gmail.com>"), if any. Untrusted like the body/subject - the
    # From: header alone is spoofable and imap-tools does no verification of
    # it.
    sender_display_name: str | None = None
    message_id: str | None = None
    message_references: str | None = None
    message_date: str | None = None
    rejection_reason: str | None = None
    body_chars: int | None = None
    trace_id: str = field(default_factory=lambda: str(uuid4()))
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


def cap_sender_name(name: str | None) -> str | None:
    """Return a stripped, length-bounded From: display name, or None.

    imap-tools' EmailAddress.name falls back to '' (not None) when the
    header carries no display name, so this normalizes that case to None
    for callers.
    """
    name = (name or "").strip()
    return name[:MAX_SENDER_NAME_CHARS] or None


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
    """True if the receiving server vouches for this message's sender auth.

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

    verdicts = {
        mech.lower(): result.lower()
        for mech, result in _AUTH_RESULT_RE.findall(header_value)
    }
    if not verdicts:
        _warn_unrecognized_auth_results(header_value)
        return False
    return any(verdicts.get(mech) == "pass" for mech in ("dkim", "spf", "auth"))


def _warn_unrecognized_auth_results(header_value: str) -> None:
    m = _AUTHSERV_ID_RE.match(header_value)
    authserv_id = (m.group(1).strip().lower() if m else "") or "unknown"
    if not _SAFE_AUTHSERV_ID_RE.fullmatch(authserv_id):
        authserv_id = "unknown"
    mechanisms = tuple(
        sorted({name.lower() for name in _AUTH_MECHANISM_RE.findall(header_value)})
    )
    key = (authserv_id, mechanisms)
    if key in _WARNED_UNRECOGNIZED_AUTH_RESULTS:
        return
    _WARNED_UNRECOGNIZED_AUTH_RESULTS.add(key)
    audit_warning_event(
        "email.auth_header_unrecognized",
        authserv_id=authserv_id,
        auth_result_mechanisms=mechanisms,
    )


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


def _first_header(msg, name: str) -> str | None:
    values = msg.headers.get(name)
    if not values:
        return None
    return values[0]


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
            message_id = clean_message_id(_first_header(msg, "message-id"))
            message_references = clean_references(_first_header(msg, "references"))
            message_date = _first_header(msg, "date")
            subject = cap_subject(msg.subject)
            sender_display_name = cap_sender_name(msg.from_values.name if msg.from_values else None)
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
                        sender_display_name=sender_display_name,
                        message_id=message_id,
                        message_references=message_references,
                        message_date=message_date,
                        auto_submitted=auto_sub[0] if auto_sub else None,
                        sender_authenticated=_is_sender_authenticated(msg),
                        rejection_reason=REJECT_BODY_OVERSIZE,
                        body_chars=exc.body_chars,
                        raw_message=raw_message,
                    )
                )
                continue
            # Near-empty bodies (e.g. a first "Hi" with everything said in the
            # subject) are deliberately NOT rejected here. They still need to
            # reach process_email so its rate-limit + first-contact-welcome
            # handling (worker/tasks.py) can run - rejecting at intake would
            # silently drop a legitimate first contact with no reply at all.
            messages.append(
                InboundMessage(
                    uid=msg.uid,
                    sender=msg.from_,
                    subject=subject,
                    body=body,
                    sender_display_name=sender_display_name,
                    message_id=message_id,
                    message_references=message_references,
                    message_date=message_date,
                    auto_submitted=auto_sub[0] if auto_sub else None,
                    sender_authenticated=_is_sender_authenticated(msg),
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
