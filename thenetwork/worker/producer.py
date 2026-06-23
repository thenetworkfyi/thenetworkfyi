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

from thenetwork.email.inbound import mark_messages_seen, poll_unseen
from thenetwork.worker.tasks import app, process_email


def _poll_and_enqueue() -> int:
    """Poll inbox, enqueue one job per message, mark seen. Assumes app is open."""
    messages = poll_unseen()   # does NOT mark seen
    count = 0
    enqueued_uids: list[str] = []
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


def run_producer_cycle() -> int:
    """Run one polling cycle for manual/CLI use; opens its own app connection."""
    with app.open():
        return _poll_and_enqueue()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="poll_inbox")
async def poll_inbox(_timestamp: int) -> int:
    """Periodic IMAP poll, runs inside the worker every minute.

    The blocking IMAP I/O is offloaded to a thread so it never stalls the
    worker's async job loop. ``queueing_lock`` keeps a slow poll from piling
    up overlapping cycles.
    """
    return await asyncio.to_thread(_poll_and_enqueue)
