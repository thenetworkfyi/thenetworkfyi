"""IMAP producer: poll unseen messages, enqueue durable Procrastinate jobs.

Durability contract: the Postgres job row is the source of truth.
The IMAP seen-flag is set AFTER the job is enqueued as cheap dedup only.
Crash between enqueue and mark-seen → job runs again (idempotent via Procrastinate).
"""
from __future__ import annotations

from thenetwork.email.inbound import mark_messages_seen, poll_unseen
from thenetwork.worker.tasks import app, process_email


def run_producer_cycle() -> int:
    """Poll inbox, enqueue one job per message, return count enqueued."""
    messages = poll_unseen()   # does NOT mark seen
    count = 0
    enqueued_uids: list[str] = []
    with app.open():
        for msg in messages:
            process_email.defer(
                sender_email=msg.sender,
                subject=msg.subject,
                body=msg.body,
            )
            enqueued_uids.append(msg.uid)
            count += 1
    # Mark seen AFTER successful enqueue — crash before here → email retried
    mark_messages_seen(enqueued_uids)
    return count
