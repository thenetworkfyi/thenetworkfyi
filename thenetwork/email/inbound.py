"""IMAP inbox polling via imap-tools."""
from __future__ import annotations

from dataclasses import dataclass

from imap_tools import AND, MailBox, MailMessageFlags

from thenetwork.settings import get_settings


@dataclass
class InboundMessage:
    uid: str
    sender: str
    subject: str
    body: str
    # RFC 3834 loop prevention headers, if present
    auto_submitted: str | None


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
                    body=msg.text or msg.html or "",
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
