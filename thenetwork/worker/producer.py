"""IMAP producer: poll unseen messages, enqueue durable Procrastinate jobs.

Durability contract: the Postgres job row is the source of truth.
The IMAP seen-flag is set AFTER the job is enqueued as cheap dedup only.
Crash between enqueue and mark-seen → job runs again (idempotent via Procrastinate).

Polling runs as a periodic task INSIDE the worker (see ``poll_inbox``), so a
single long-running worker process handles intake, processing, and proactive
scans. ``run_producer_cycle`` remains for manual/one-shot CLI use.
"""

from __future__ import annotations

import asyncio
import base64

from disposable_email import is_disposable

from thenetwork.audit import (
    audit_event,
    audit_run,
    audit_sender,
    audit_span,
    audit_trace,
)
from thenetwork.email.dedup import (
    is_message_processed,
    mark_message_processed,
    unmark_message_processed,
)
from thenetwork.email.inbound import (
    MailboxKind,
    mark_messages_seen,
    poll_unseen,
    relay_mailbox_configured,
)
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.worker.tasks import app, process_email

REJECT_DISPOSABLE_DOMAIN = "disposable_domain"


def _poll_mailbox_and_enqueue(mailbox: MailboxKind) -> int:
    """Poll one inbox, enqueue its messages, then mark its UIDs seen."""
    messages = poll_unseen(mailbox=mailbox)  # does NOT mark seen
    count = 0
    handled_uids: list[str] = []
    for msg in messages:
        with (
            audit_trace(msg.trace_id),
            audit_sender(optional_sender_identifier(msg.sender)),
        ):
            auto_submitted = msg.auto_submitted
            body_chars = msg.body_chars if msg.body_chars is not None else len(msg.body)
            if msg.rejection_reason:
                audit_event(
                    "intake.message_rejected",
                    sender_present=bool(msg.sender),
                    subject_chars=len(msg.subject),
                    body_chars=body_chars,
                    auto_submitted_present=bool(auto_submitted),
                    header_names=["from", "subject", "auto-submitted"],
                    reason=msg.rejection_reason,
                )
                handled_uids.append(msg.uid)
                continue
            if is_disposable(msg.sender):
                audit_event(
                    "intake.message_rejected",
                    sender_present=bool(msg.sender),
                    subject_chars=len(msg.subject),
                    body_chars=body_chars,
                    auto_submitted_present=bool(auto_submitted),
                    header_names=["from", "subject", "auto-submitted"],
                    reason=REJECT_DISPOSABLE_DOMAIN,
                )
                handled_uids.append(msg.uid)
                continue
            if msg.message_id and is_message_processed(msg.message_id):
                # A job already ran for this Message-ID - the \Seen flag was
                # reset after the fact (mail client, sync bug, manual
                # recovery). Re-enqueuing would re-run the agent against an
                # already-handled email, risking duplicate replies, memories,
                # or outbound sends. Just re-mark seen and move on.
                audit_event(
                    "intake.message_duplicate_skipped",
                    sender_present=bool(msg.sender),
                    subject_chars=len(msg.subject),
                    body_chars=body_chars,
                )
                handled_uids.append(msg.uid)
                continue
            audit_event(
                "intake.message_received",
                sender_present=bool(msg.sender),
                subject_chars=len(msg.subject),
                body_chars=body_chars,
                auto_submitted_present=bool(auto_submitted),
                header_names=["from", "subject", "auto-submitted"],
            )
            raw_message_b64 = (
                base64.b64encode(msg.raw_message).decode() if msg.raw_message else None
            )
            job_kwargs = {
                "sender_email": msg.sender,
                "subject": msg.subject,
                "body": msg.body,
                "sender_authenticated": msg.sender_authenticated,
                "sender_display_name": msg.sender_display_name,
                "raw_message_b64": raw_message_b64,
                "trace_id": msg.trace_id,
            }
            if msg.recipient_address:
                job_kwargs["recipient_address"] = msg.recipient_address
            if msg.message_id:
                job_kwargs["inbound_message_id"] = msg.message_id
                job_kwargs["inbound_references"] = msg.message_references
                job_kwargs["inbound_body_for_quote"] = msg.body
                job_kwargs["inbound_date"] = msg.message_date
            if msg.message_id:
                mark_message_processed(msg.message_id)
            try:
                process_email.defer(**job_kwargs)
            except Exception:
                if msg.message_id:
                    unmark_message_processed(msg.message_id)
                raise
            handled_uids.append(msg.uid)
            count += 1
    # Mark seen only after each message has either been enqueued or
    # intentionally rejected. Crash before here means the email is retried.
    mark_messages_seen(handled_uids, mailbox=mailbox)
    return count


def _poll_and_enqueue() -> int:
    """Poll configured inboxes and enqueue one job per message."""
    with audit_run(), audit_span("producer.poll"):
        relay_configured = relay_mailbox_configured()
        count = _poll_mailbox_and_enqueue("primary")
        if relay_configured:
            count += _poll_mailbox_and_enqueue("relay")
        audit_event("producer.poll_completed", message_count=count, outcome="success")
        return count


def run_producer_cycle() -> int:
    """Run one polling cycle for manual/CLI use; opens its own app connection."""
    with app.open():
        return _poll_and_enqueue()


@app.periodic(cron="* * * * *", periodic_id="poll_inbox")
@app.task(queueing_lock="poll_inbox")
async def poll_inbox(timestamp: int) -> int:
    """Periodic IMAP poll, runs inside the worker every minute.

    The blocking IMAP I/O is offloaded to a thread so it never stalls the
    worker's async job loop. ``queueing_lock`` keeps a slow poll from piling
    up overlapping cycles.
    """
    return await asyncio.to_thread(_poll_and_enqueue)
