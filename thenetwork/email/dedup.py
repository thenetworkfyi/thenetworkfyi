"""Idempotency guard for inbound intake, keyed on Message-ID.

The IMAP \\Seen flag is cheap dedup only (see worker/producer.py) - it can be
cleared by a mail client, sync bug, or manual recovery step well after a
message was already fully handled, at which point the next poll would treat
it as newly arrived. `processed_messages` is the durable record that a given
Message-ID already got a `process_email` job, so re-seeing it can't cause the
agent to re-run, re-reply, or re-dispatch an introduction for the same
physical email.
"""
from __future__ import annotations

from sqlalchemy import text

from thenetwork.db.session import get_engine


def is_message_processed(message_id: str) -> bool:
    """Return True if a job was already enqueued for this Message-ID before."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM processed_messages WHERE message_id = :message_id"),
            {"message_id": message_id},
        ).first()
    return row is not None


def mark_message_processed(message_id: str) -> None:
    """Durably reserve a Message-ID before its job is enqueued."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO processed_messages (message_id, processed_at)
                VALUES (:message_id, now())
                ON CONFLICT (message_id) DO NOTHING
                """
            ),
            {"message_id": message_id},
        )


def unmark_message_processed(message_id: str) -> None:
    """Release an intake reservation when deferring its job fails."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM processed_messages WHERE message_id = :message_id"),
            {"message_id": message_id},
        )
