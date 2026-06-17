"""IMAP inbox polling via imap-tools."""
from __future__ import annotations

from dataclasses import dataclass

from imap_tools import MailBox, AND

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
    """Return True if inbound mail carries Auto-Submitted header (RFC 3834)."""
    auto = msg.headers.get("auto-submitted")
    if auto and auto[0].lower() != "no":
        return True
    # Also skip List-*/Precedence: bulk/list/junk
    precedence = msg.headers.get("precedence")
    if precedence and precedence[0].lower() in ("bulk", "list", "junk"):
        return True
    return False


def poll_unseen() -> list[InboundMessage]:
    """Fetch and return all unseen messages, marking them as seen.

    Skips auto-generated messages per RFC 3834 to prevent mail loops.
    """
    s = get_settings()
    messages: list[InboundMessage] = []

    with MailBox(s.imap_host, s.imap_port).login(s.email_account, s.email_password) as mb:
        for msg in mb.fetch(AND(seen=False), mark_seen=True, bulk=True):
            if _is_auto_message(msg):
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
