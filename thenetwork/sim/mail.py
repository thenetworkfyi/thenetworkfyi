"""Email transport helpers for simulation harness runs."""
from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Any
from uuid import uuid4
from unittest.mock import patch

from bs4 import BeautifulSoup

from thenetwork.email.inbound import cap_body, cap_sender_name, cap_subject
from thenetwork.email.threading import clean_message_id, clean_references
from thenetwork.worker.tasks import process_email


ProcessEmailCallable = Callable[..., Awaitable[None]]


@dataclass
class SimPostOffice:
    """In-memory mailbox keyed by normalized recipient address."""

    _messages: dict[str, list[EmailMessage]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def deliver(self, message: EmailMessage) -> None:
        recipients = _recipient_addresses(message)
        if not recipients:
            return
        for recipient in recipients:
            self._messages[recipient].append(deepcopy(message))

    def messages_for(self, address: str) -> tuple[EmailMessage, ...]:
        return tuple(self._messages.get(_normalize_address(address), ()))

    def pop_all(self, address: str) -> tuple[EmailMessage, ...]:
        return tuple(self._messages.pop(_normalize_address(address), ()))

    @property
    def all_messages(self) -> tuple[EmailMessage, ...]:
        messages: list[EmailMessage] = []
        for bucket in self._messages.values():
            messages.extend(bucket)
        return tuple(messages)


class _PostOfficeSMTP:
    def __init__(self, post_office: SimPostOffice) -> None:
        self.post_office = post_office

    def __call__(self, *_args: Any, **_kwargs: Any) -> "_PostOfficeSMTP":
        return self

    def __enter__(self) -> "_PostOfficeSMTP":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def ehlo(self) -> None:
        return None

    def starttls(self) -> None:
        return None

    def login(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def send_message(self, message: EmailMessage) -> None:
        self.post_office.deliver(message)


@contextmanager
def capture_outbound(post_office: SimPostOffice) -> Iterator[SimPostOffice]:
    """Capture outbound mail produced by `thenetwork.email.outbound.send_reply`.

    The seam is deliberately the `smtplib.SMTP` object used inside
    `email/outbound.py`, so subject handling, threading headers, growth footer,
    Message-ID, Date, and Auto-Submitted are still composed by production code.
    IMAP Sent append is disabled because the post office is the run's mail log.
    """
    smtp = _PostOfficeSMTP(post_office)
    with patch("thenetwork.email.outbound.smtplib.SMTP", smtp), patch(
        "thenetwork.email.outbound._append_to_sent", return_value=None
    ):
        yield post_office


@dataclass(frozen=True)
class InboundDelivery:
    sender_email: str
    subject: str
    body: str
    trace_id: str
    message_id: str | None


async def deliver_inbound(
    message: EmailMessage,
    *,
    sender_authenticated: bool = True,
    process: ProcessEmailCallable | None = None,
    trace_id: str | None = None,
) -> InboundDelivery:
    """Call the worker's `process_email` task directly for one EmailMessage."""
    sender_display_name, sender_email = parseaddr(message.get("From", ""))
    sender_email = sender_email.strip()
    if not sender_email:
        raise ValueError("inbound message must have a From address")

    subject = cap_subject(message.get("Subject", ""))
    body = cap_body(_extract_body(message))
    message_id = clean_message_id(message.get("Message-ID"))
    references = clean_references(message.get("References"))
    message_date = message.get("Date")
    raw_message_b64 = (
        base64.b64encode(message.as_bytes()).decode("ascii")
        if subject.strip().lower().startswith("admin:")
        else None
    )
    resolved_trace_id = trace_id or str(uuid4())
    process_func = process or process_email.func

    await process_func(
        sender_email=sender_email,
        subject=subject,
        body=body,
        sender_authenticated=sender_authenticated,
        sender_display_name=cap_sender_name(sender_display_name),
        raw_message_b64=raw_message_b64,
        inbound_message_id=message_id,
        inbound_references=references,
        inbound_body_for_quote=body,
        inbound_date=message_date,
        trace_id=resolved_trace_id,
    )

    return InboundDelivery(
        sender_email=sender_email,
        subject=subject,
        body=body,
        trace_id=resolved_trace_id,
        message_id=message_id,
    )


def _recipient_addresses(message: EmailMessage) -> tuple[str, ...]:
    header_values = [
        value
        for name in ("to", "cc", "bcc")
        for value in message.get_all(name, [])
    ]
    addresses = {
        _normalize_address(address)
        for _display_name, address in getaddresses(header_values)
        if address
    }
    return tuple(sorted(addresses))


def _normalize_address(address: str) -> str:
    return parseaddr(address)[1].strip().lower()


def _extract_body(message: EmailMessage) -> str:
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content()
    html = message.get_body(preferencelist=("html",))
    if html is not None:
        return _html_to_text(html.get_content())
    if message.get_content_maintype() == "text":
        return message.get_content()
    return ""


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    for tag in soup(("head", "script", "style", "template", "title")):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())
