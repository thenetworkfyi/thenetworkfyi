"""Procrastinate task definitions for the email processing worker.

The worker is Postgres-native (LISTEN/NOTIFY + SKIP LOCKED, no Redis/broker).
Retries and backoff are Procrastinate's responsibility — no hand-rolled loops.
"""
from __future__ import annotations

import base64

import procrastinate
from sqlmodel import select

from thenetwork.admin.auth import extract_body_text, extract_command, verify_admin_request
from thenetwork.admin.commands import handle_admin_command
from thenetwork.agent.core import run_agent_for_email
from thenetwork.audit import audit_event, audit_run, audit_span, configure_audit_logging
from thenetwork.db.models import Person
from thenetwork.db.session import get_session
from thenetwork.email.inbound import (
    REJECT_BODY_EMPTY,
    REJECT_BODY_OVERSIZE,
    BodyTooLargeError,
    cap_body,
    cap_subject,
    is_near_empty_body,
)
from thenetwork.email.outbound import send_reply
from thenetwork.security.content_scan import scan_content
from thenetwork.security.rate_limit import check_rate_limit
from thenetwork.settings import get_settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(),
    # All modules that register tasks/periodics must be imported so the worker
    # discovers them: email processing (here), IMAP polling, proactive scans.
    import_paths=[
        "thenetwork.worker.tasks",
        "thenetwork.worker.producer",
        "thenetwork.worker.proactive",
    ],
)

REJECT_RATE_LIMIT = "rate_limit"
REJECT_CONTENT_SCAN = "content_scan"

_INFRASTRUCTURE_REJECTION_REPLIES = {
    REJECT_BODY_OVERSIZE: (
        "We could not process your email because the message body was too large. "
        "Please send a shorter message and try again."
    ),
    REJECT_RATE_LIMIT: (
        "We could not process your email because this address is sending too many "
        "messages right now. Please wait and try again later."
    ),
    REJECT_CONTENT_SCAN: (
        "We could not process your email because it was blocked by an automated "
        "safety scan. Please revise the message and try again."
    ),
}


def _is_known_authenticated_sender(sender_email: str, sender_authenticated: bool) -> bool:
    if not sender_authenticated:
        return False

    with get_session() as session:
        sender_id = session.exec(
            select(Person.id).where(Person.email == sender_email)
        ).first()

    return sender_id is not None


def _send_infrastructure_rejection_reply(
    *,
    sender_email: str,
    subject: str,
    sender_authenticated: bool,
    reason: str,
) -> None:
    body_text = _INFRASTRUCTURE_REJECTION_REPLIES[reason]
    if not _is_known_authenticated_sender(sender_email, sender_authenticated):
        return

    send_reply(
        to_address=sender_email,
        subject=f"Re: {subject}",
        body_text=body_text,
        include_footer=False,
    )


@app.task(retry=procrastinate.RetryStrategy(max_attempts=3, wait=60))
async def process_email(
    sender_email: str,
    subject: str,
    body: str,
    sender_authenticated: bool = False,
    raw_message_b64: str | None = None,
) -> None:
    """Procrastinate worker task: run the agent for one inbound email.

    Checks rate limit and optional content scan before handing off.
    Admin requests are handled directly; regular mail goes to the agent.
    Agent runs with sender's existing user_id (None if first contact).

    ``sender_authenticated`` reflects the receiving server's DKIM/SPF
    verdict on the From: header (see email/inbound.py). An unauthenticated
    From is never resolved to an existing Person — that header alone is
    spoofable, and treating a spoofed sender as a known identity would let
    anyone impersonate a real user (write memories in their name, dispatch
    email as them, or burn their rate-limit quota).
    """
    subject = cap_subject(subject)
    original_body_chars = len(body)
    with audit_run(), audit_span(
        "worker.process_email",
        sender_present=bool(sender_email),
        subject_chars=len(subject),
        body_chars=original_body_chars,
        sender_authenticated=sender_authenticated,
    ):
        try:
            body = cap_body(body)
        except BodyTooLargeError:
            audit_event("worker.message_rejected", reason=REJECT_BODY_OVERSIZE)
            _send_infrastructure_rejection_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                reason=REJECT_BODY_OVERSIZE,
            )
            return

        if is_near_empty_body(body):
            audit_event("worker.message_rejected", reason=REJECT_BODY_EMPTY)
            return

        if not check_rate_limit(
            sender_email,
            sender_authenticated=sender_authenticated,
        ):
            audit_event("worker.message_rejected", reason=REJECT_RATE_LIMIT)
            _send_infrastructure_rejection_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                reason=REJECT_RATE_LIMIT,
            )
            return

        is_safe, _ = scan_content(body)
        if not is_safe:
            audit_event("worker.message_rejected", reason=REJECT_CONTENT_SCAN)
            _send_infrastructure_rejection_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                reason=REJECT_CONTENT_SCAN,
            )
            return

        raw_message = base64.b64decode(raw_message_b64) if raw_message_b64 else None
        verified_body = verify_admin_request(sender_email, subject, raw_message)
        if verified_body is not None:
            command = extract_command(verified_body)
            body_text = extract_body_text(verified_body)
            reply = await handle_admin_command(command, body_text)
            send_reply(
                to_address=sender_email,
                subject=f"Re: {subject}",
                body_text=reply,
                include_footer=False,
            )
            return

        sender_user_id: str | None = None
        with get_session() as session:
            profile = session.exec(
                select(Person).where(Person.email == sender_email)
            ).first()
            if profile and sender_authenticated:
                sender_user_id = profile.id

        audit_event(
            "database.action",
            action="lookup",
            record_type="person",
            outcome="found" if profile is not None else "not_found",
        )

        if not sender_authenticated and profile is None:
            audit_event(
                "worker.message_rejected",
                reason="unauthenticated_unknown_sender",
            )
            return

        await run_agent_for_email(
            sender_email=sender_email,
            sender_user_id=sender_user_id,
            sender_authenticated=sender_authenticated,
            email_subject=subject,
            email_body=body,
        )


async def run_worker() -> None:
    """Start the Procrastinate worker with concurrency from settings.

    Procrastinate handles graceful shutdown: on SIGINT/SIGTERM it stops
    fetching new jobs and lets in-flight jobs finish before exiting. Give the
    process a generous stop grace period (see compose ``stop_grace_period``).
    """
    s = get_settings()
    dsn = s.database_url.replace("postgresql+psycopg://", "postgresql://")
    async with app.open_async(conninfo=dsn):
        await app.run_worker_async(concurrency=s.worker_concurrency)


def main() -> None:
    """Console entrypoint: run the long-lived worker (intake + processing + scans)."""
    import asyncio

    configure_audit_logging()
    asyncio.run(run_worker())


def producer_main() -> None:
    """Console entrypoint: run a single IMAP poll cycle (for manual/cron use)."""
    from thenetwork.worker.producer import run_producer_cycle

    configure_audit_logging()
    print(run_producer_cycle())
