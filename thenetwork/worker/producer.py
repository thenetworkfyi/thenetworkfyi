"""IMAP producer: poll unseen messages, enqueue durable Procrastinate jobs.

Durability contract: the Postgres job row is the source of truth.
The IMAP seen-flag is set AFTER the job is enqueued as cheap dedup only.
Crash between enqueue and mark-seen → job runs again (idempotent via Procrastinate).
"""
from __future__ import annotations

from thenetwork.email.inbound import poll_unseen
from thenetwork.worker.tasks import app, process_email


def run_producer_cycle() -> int:
    """Poll inbox, enqueue one job per message, return count enqueued."""
    messages = poll_unseen()
    count = 0
    with app.open():
        for msg in messages:
            process_email.defer(
                sender_email=msg.sender,
                subject=msg.subject,
                body=msg.body,
            )
            count += 1
    return count
