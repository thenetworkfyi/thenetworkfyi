"""Procrastinate task definitions for the email processing worker.

The worker is Postgres-native (LISTEN/NOTIFY + SKIP LOCKED, no Redis/broker).
Retries and backoff are Procrastinate's responsibility - no hand-rolled loops.
"""
from __future__ import annotations

import base64

import procrastinate
from limits import parse, strategies
from sqlmodel import select

from thenetwork.admin.auth import extract_body_text, extract_command, verify_admin_request
from thenetwork.admin.commands import handle_admin_command
from thenetwork.agent.core import run_agent_for_email
from thenetwork.audit import (
    audit_event,
    audit_run,
    audit_sender,
    audit_span,
    audit_trace,
    configure_audit_logging,
)
from thenetwork.db.models import BannedEmail, Person
from thenetwork.db.session import get_session
from thenetwork.email.inbound import (
    REJECT_BODY_EMPTY,
    REJECT_BODY_OVERSIZE,
    BodyTooLargeError,
    cap_body,
    cap_subject,
    is_near_empty_body,
)
from thenetwork.email.outbound import (
    FIRST_CONTACT_WELCOME_REPLY,
    _direct_reply_kwargs,
    _thread_headers,
    notify_admins,
    reply_subject,
    send_reply,
)
from thenetwork.memory.sanitize import assert_presidio_ready
from thenetwork.introductions import process_consent_reply
from thenetwork.security.content_scan import scan_content
from thenetwork.security.rate_limit import (
    PostgresFixedWindowStorage,
    check_rate_limit,
    normalize_rate_limit_identity,
)
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.settings import get_settings

app = procrastinate.App(
    # Procrastinate's own DSN (plain postgresql://); Procrastinate 3.x takes
    # conninfo on the connector, not on App.open_async.
    connector=procrastinate.PsycopgConnector(
        conninfo=get_settings().database_url.replace(
            "postgresql+psycopg://", "postgresql://"
        )
    ),
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
_WELCOME_LIMIT = parse("1/day")
_welcome_limiter: strategies.FixedWindowRateLimiter | None = None
_welcome_storage: PostgresFixedWindowStorage | None = None


def _get_welcome_limiter() -> tuple[strategies.FixedWindowRateLimiter, object]:
    global _welcome_limiter, _welcome_storage
    if _welcome_limiter is None:
        _welcome_storage = PostgresFixedWindowStorage()
        _welcome_limiter = strategies.FixedWindowRateLimiter(_welcome_storage)
    return _welcome_limiter, _welcome_storage


def _hit_welcome_quota(sender_email: str) -> bool:
    limiter, _ = _get_welcome_limiter()
    identity = normalize_rate_limit_identity(sender_email)
    try:
        return limiter.hit(_WELCOME_LIMIT, f"welcome:first-contact:{identity}")
    except Exception:
        return False


def _trace_kwargs(trace_id: str | None) -> dict[str, str]:
    return {"trace_id": trace_id} if trace_id else {}


def _is_known_authenticated_sender(sender_email: str, sender_authenticated: bool) -> bool:
    if not sender_authenticated:
        return False

    with get_session() as session:
        sender_id = session.exec(
            select(Person.id).where(Person.email == sender_email)
        ).first()

    return sender_id is not None


def _sender_id_for_authenticated_sender(
    sender_email: str,
    sender_authenticated: bool,
) -> str | None:
    if not sender_authenticated:
        return None

    with get_session() as session:
        return session.exec(
            select(Person.id).where(Person.email == sender_email)
        ).first()


def _send_infrastructure_rejection_reply(
    *,
    sender_email: str,
    subject: str,
    sender_authenticated: bool,
    reason: str,
    inbound_message_id: str | None = None,
    inbound_references: str | None = None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    trace_id: str | None = None,
) -> None:
    body_text = _INFRASTRUCTURE_REJECTION_REPLIES[reason]
    if not _is_known_authenticated_sender(sender_email, sender_authenticated):
        return

    send_reply(
        to_address=sender_email,
        subject=f"Re: {subject}",
        body_text=body_text,
        include_footer=False,
        **_trace_kwargs(trace_id),
        **_direct_reply_kwargs(
            inbound_message_id=inbound_message_id,
            inbound_body_for_quote=inbound_body_for_quote,
            inbound_date=inbound_date,
            inbound_references=inbound_references,
        ),
    )


def _send_first_contact_welcome_reply(
    *,
    sender_email: str,
    subject: str,
    sender_authenticated: bool,
    inbound_message_id: str | None = None,
    inbound_references: str | None = None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    trace_id: str | None = None,
) -> bool:
    if not sender_authenticated:
        return False
    if _sender_id_for_authenticated_sender(sender_email, sender_authenticated) is not None:
        return False
    if not _hit_welcome_quota(sender_email):
        return False

    send_reply(
        to_address=sender_email,
        subject=reply_subject(subject, fallback="How to join"),
        body_text=FIRST_CONTACT_WELCOME_REPLY,
        include_footer=False,
        **_trace_kwargs(trace_id),
        **_direct_reply_kwargs(
            inbound_message_id=inbound_message_id,
            inbound_body_for_quote=inbound_body_for_quote,
            inbound_date=inbound_date,
            inbound_references=inbound_references,
        ),
    )
    return True


_PROCESS_EMAIL_MAX_ATTEMPTS = 3


@app.task(
    retry=procrastinate.RetryStrategy(max_attempts=_PROCESS_EMAIL_MAX_ATTEMPTS, wait=60),
    pass_context=True,
)
async def process_email(
    context: procrastinate.JobContext | None = None,
    *,
    sender_email: str,
    subject: str,
    body: str,
    sender_authenticated: bool = False,
    sender_display_name: str | None = None,
    raw_message_b64: str | None = None,
    inbound_message_id: str | None = None,
    inbound_references: str | None = None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    trace_id: str | None = None,
    is_proactive: bool = False,
    proactive_candidate_id: str | None = None,
) -> None:
    """Procrastinate worker task: run the agent for one inbound email.

    Checks rate limit and optional content scan before handing off.
    Admin requests are handled directly; regular mail goes to the agent.
    Agent runs with sender's existing user_id (None if first contact).

    ``sender_authenticated`` reflects the receiving server's DKIM/SPF
    verdict on the From: header (see email/inbound.py). An unauthenticated
    From is never resolved to an existing Person - that header alone is
    spoofable, and treating a spoofed sender as a known identity would let
    anyone impersonate a real user (write memories in their name, dispatch
    email as them, or burn their rate-limit quota).
    """
    subject = cap_subject(subject)
    original_body_chars = len(body)
    with audit_run(), audit_trace(trace_id), audit_sender(
        optional_sender_identifier(sender_email)
    ), audit_span(
        "worker.process_email",
        sender_present=bool(sender_email),
        subject_chars=len(subject),
        body_chars=original_body_chars,
        sender_authenticated=sender_authenticated,
    ):
        with get_session() as session:
            banned = session.get(
                BannedEmail, normalize_rate_limit_identity(sender_email)
            )
            if banned:
                audit_event("worker.message_rejected", reason="banned")
                return

        try:
            body = cap_body(body)
        except BodyTooLargeError:
            audit_event("worker.message_rejected", reason=REJECT_BODY_OVERSIZE)
            _send_infrastructure_rejection_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                reason=REJECT_BODY_OVERSIZE,
                inbound_message_id=inbound_message_id,
                inbound_references=inbound_references,
                inbound_body_for_quote=inbound_body_for_quote or body,
                inbound_date=inbound_date,
                trace_id=trace_id,
            )
            return

        if is_near_empty_body(body):
            rate_limit_kwargs = {"sender_authenticated": sender_authenticated}
            if is_proactive:
                rate_limit_kwargs["skip_sender_limit"] = True
            if not check_rate_limit(
                sender_email,
                **rate_limit_kwargs,
            ):
                audit_event("worker.message_rejected", reason=REJECT_RATE_LIMIT)
                return
            audit_event("worker.message_rejected", reason=REJECT_BODY_EMPTY)
            welcomed = _send_first_contact_welcome_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                inbound_message_id=inbound_message_id,
                inbound_references=inbound_references,
                inbound_body_for_quote=inbound_body_for_quote or body,
                inbound_date=inbound_date,
                trace_id=trace_id,
            )
            if welcomed:
                audit_event("worker.first_contact_welcome_sent")
            return

        rate_limit_kwargs = {"sender_authenticated": sender_authenticated}
        if is_proactive:
            rate_limit_kwargs["skip_sender_limit"] = True
        if not check_rate_limit(sender_email, **rate_limit_kwargs):
            audit_event("worker.message_rejected", reason=REJECT_RATE_LIMIT)
            _send_infrastructure_rejection_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                reason=REJECT_RATE_LIMIT,
                inbound_message_id=inbound_message_id,
                inbound_references=inbound_references,
                inbound_body_for_quote=inbound_body_for_quote or body,
                inbound_date=inbound_date,
                trace_id=trace_id,
            )
            return

        is_safe, scan_reason = scan_content(body)
        if not is_safe:
            audit_event("worker.message_rejected", reason=scan_reason)
            _send_infrastructure_rejection_reply(
                sender_email=sender_email,
                subject=subject,
                sender_authenticated=sender_authenticated,
                reason=REJECT_CONTENT_SCAN,
                inbound_message_id=inbound_message_id,
                inbound_references=inbound_references,
                inbound_body_for_quote=inbound_body_for_quote or body,
                inbound_date=inbound_date,
                trace_id=trace_id,
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
                **_trace_kwargs(trace_id),
                **_thread_headers(inbound_message_id, inbound_references),
            )
            return

        sender_user_id = _sender_id_for_authenticated_sender(
            sender_email,
            sender_authenticated,
        )

        audit_event(
            "database.action",
            action="lookup",
            record_type="person",
            outcome="found" if sender_user_id is not None else "not_found",
        )

        consent_result = process_consent_reply(
            sender_person_id=sender_user_id,
            sender_authenticated=sender_authenticated,
            subject=subject,
            body=body,
            trace_id=trace_id,
        )
        if consent_result.handled:
            return

        if not sender_authenticated and sender_user_id is None:
            audit_event(
                "worker.message_rejected",
                reason="unauthenticated_unknown_sender",
            )
            return

        agent_kwargs = {
            "sender_email": sender_email,
            "sender_user_id": sender_user_id,
            "sender_authenticated": sender_authenticated,
            "email_subject": subject,
            "email_body": body,
            "sender_display_name": sender_display_name,
            "is_proactive": is_proactive,
        }
        if proactive_candidate_id:
            agent_kwargs["proactive_candidate_id"] = proactive_candidate_id
        if trace_id:
            agent_kwargs["trace_id"] = trace_id
        if inbound_message_id:
            agent_kwargs["inbound_message_id"] = inbound_message_id
            agent_kwargs["inbound_references"] = inbound_references
            agent_kwargs["inbound_body_for_quote"] = inbound_body_for_quote or body
            agent_kwargs["inbound_date"] = inbound_date
        try:
            await run_agent_for_email(**agent_kwargs)
        except Exception as exc:
            audit_event(
                "worker.agent_failed",
                outcome="error",
                error_type=type(exc).__name__,
            )
            if context is not None and context.job.attempts >= _PROCESS_EMAIL_MAX_ATTEMPTS:
                notify_admins(
                    get_settings(),
                    "[The Network] Agent processing failed",
                    "The agent failed on its final processing attempt. "
                    "Use the trace id in the audit log to investigate.",
                    trace_id=trace_id,
                )
            raise


async def run_worker() -> None:
    """Start the Procrastinate worker with concurrency from settings.

    Procrastinate handles graceful shutdown: on SIGINT/SIGTERM it stops
    fetching new jobs and lets in-flight jobs finish before exiting. Give the
    process a generous stop grace period (see compose ``stop_grace_period``).
    """
    s = get_settings()
    async with app.open_async():
        await app.run_worker_async(concurrency=s.worker_concurrency)


def main() -> None:
    """Console entrypoint: run the long-lived worker (intake + processing + scans)."""
    import asyncio
    from thenetwork.embed.embeddings import validate_embedding_configuration

    validate_embedding_configuration()
    configure_audit_logging()
    assert_presidio_ready()
    asyncio.run(run_worker())


def producer_main() -> None:
    """Console entrypoint: run a single IMAP poll cycle (for manual/cron use)."""
    from thenetwork.embed.embeddings import validate_embedding_configuration
    from thenetwork.worker.producer import run_producer_cycle

    validate_embedding_configuration()
    configure_audit_logging()
    print(run_producer_cycle())
