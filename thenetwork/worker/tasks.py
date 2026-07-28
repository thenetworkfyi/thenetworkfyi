"""Procrastinate task definitions for the email processing worker.

The worker is Postgres-native (LISTEN/NOTIFY + SKIP LOCKED, no Redis/broker).
Retries and backoff are Procrastinate's responsibility - no hand-rolled loops.
"""

from __future__ import annotations

import base64

import procrastinate
from sqlmodel import select

from thenetwork.admin.auth import (
    extract_body_text,
    extract_command,
    is_admin_request_candidate,
    verify_admin_request,
)
from thenetwork.admin.commands import handle_admin_command
from thenetwork.agent.core import run_agent_for_email
from thenetwork.agent.deps import AgentCapabilities
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
    MailboxKind,
    REJECT_BODY_OVERSIZE,
    BodyTooLargeError,
    cap_body,
    cap_subject,
)
from thenetwork.email.intake_control import is_primary_intake_paused
from thenetwork.email.outbound import (
    _direct_reply_kwargs,
    _thread_headers,
    send_relay_email,
    send_reply,
)
from thenetwork.email.relay import (
    build_relay_address,
    is_relay_address_candidate,
    parse_relay_address,
    resolve_relay_destination,
)
from thenetwork.email.render import (
    FixedEmailTemplate,
    InfrastructureRejectionEmailContext,
    InfrastructureRejectionReason,
)
from thenetwork.llm_observability import observe_email_lifecycle
from thenetwork.introductions import process_consent_reply
from thenetwork.memory.sanitize import assert_sanitizer_ready
from thenetwork.memory.sent_email import record_sent_email_memories
from thenetwork.security.content_scan import scan_content
from thenetwork.security.rate_limit import (
    check_rate_limit,
    normalize_rate_limit_identity,
)
from thenetwork.security.token_budget import check_daily_token_budget
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.settings import get_settings
from thenetwork.worker.metrics import record_job_exhausted

app = procrastinate.App(
    # Procrastinate's own DSN (plain postgresql://); Procrastinate 3.x takes
    # conninfo on the connector, not on App.open_async.
    connector=procrastinate.PsycopgConnector(
        conninfo=get_settings().database_url.replace(
            "postgresql+psycopg://", "postgresql://"
        )
    ),
    # All modules that register tasks/periodics must be imported so the worker
    # discovers them: email processing (here), IMAP polling, proactive scans,
    # and the independent event-recommendation scan.
    import_paths=[
        "thenetwork.worker.tasks",
        "thenetwork.worker.producer",
        "thenetwork.worker.proactive",
        "thenetwork.worker.event_scan",
        "thenetwork.worker.abuse_judge",
    ],
)

REJECT_RATE_LIMIT = "rate_limit"
REJECT_CONTENT_SCAN = "content_scan"
REJECT_ADMIN_AUTH = "admin_auth_failed"


def _trace_kwargs(trace_id: str | None) -> dict[str, str]:
    return {"trace_id": trace_id} if trace_id else {}


def _is_known_authenticated_sender(
    sender_email: str, sender_authenticated: bool
) -> bool:
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
    if not _is_known_authenticated_sender(sender_email, sender_authenticated):
        return

    send_reply(
        to_address=sender_email,
        subject=f"Re: {subject}",
        fixed_template=FixedEmailTemplate.INFRASTRUCTURE_REJECTION,
        fixed_context=InfrastructureRejectionEmailContext(
            InfrastructureRejectionReason(reason)
        ),
        **_trace_kwargs(trace_id),
        **_direct_reply_kwargs(
            inbound_message_id=inbound_message_id,
            inbound_body_for_quote=inbound_body_for_quote,
            inbound_date=inbound_date,
            inbound_references=inbound_references,
        ),
    )


def _consent_remainder_body(remainder: str, outcome: str | None) -> str:
    """Frame consent-reply text the server path would otherwise discard.

    The consent decision itself was already recorded and acknowledged with a
    fixed reply before any model ran; only the sender's own additional text is
    forwarded, so the agent can remember it or answer it without re-acting on
    the consent decision.
    """
    return (
        "[System note] This message was a reply to an introduction consent "
        f"request. The server already recorded the decision (outcome: {outcome}) "
        "and sent any fixed acknowledgment, so do not act on the consent "
        "decision or acknowledge it again. The sender's reply also carried "
        "their own additional text below - treat it as a normal inbound "
        "message from them: remember new facts, answer a genuine question "
        "briefly, or do nothing.\n\n"
        f"{remainder}"
    )


_PROCESS_EMAIL_MAX_ATTEMPTS = 3


@app.task(
    retry=procrastinate.RetryStrategy(
        max_attempts=_PROCESS_EMAIL_MAX_ATTEMPTS, wait=60
    ),
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
    attachment_count: int = 0,
    recipient_address: str | None = None,
    raw_message_b64: str | None = None,
    inbound_message_id: str | None = None,
    inbound_references: str | None = None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    trace_id: str | None = None,
    is_proactive: bool = False,
    proactive_candidate_id: str | None = None,
    proactive_event_id: str | None = None,
    proactive_event_version: int | None = None,
    source_mailbox: MailboxKind | None = None,
    intake_observed_at_epoch_seconds: float | None = None,
    capabilities: AgentCapabilities | None = None,
) -> None:
    """Procrastinate worker task: run the agent for one inbound email.

    Checks rate limit and optional content scan before handing off.
    Admin requests are handled directly; regular mail goes to the agent.
    Agent runs with sender's existing user_id (None if first contact).

    ``capabilities`` is never set by production `.defer()` callers - it is an
    optional test-only override forwarded to `run_agent_for_email`, letting
    tests exercise this task end to end without patching module globals.

    ``sender_authenticated`` reflects the third-party IMAP provider's DKIM/SPF
    verdict on the From: header (see email/inbound.py). An unauthenticated
    From is never resolved to an existing Person - that header alone is
    spoofable, and treating a spoofed sender as a known identity would let
    anyone impersonate a real user (write memories in their name, dispatch
    email as them, or burn their rate-limit quota).
    """
    subject = cap_subject(subject)
    original_body_chars = len(body)
    with (
        audit_run(),
        audit_trace(trace_id),
        audit_sender(optional_sender_identifier(sender_email)),
        observe_email_lifecycle(intake_observed_at_epoch_seconds),
        audit_span(
            "worker.process_email",
            sender_present=bool(sender_email),
            subject_chars=len(subject),
            body_chars=original_body_chars,
            sender_authenticated=sender_authenticated,
        ),
    ):
        with get_session() as session:
            banned = session.get(
                BannedEmail, normalize_rate_limit_identity(sender_email)
            )
            if banned:
                audit_event("worker.message_rejected", reason="banned")
                return

        relay_domain = get_settings().relay_domain if recipient_address else ""
        relay_candidate = is_relay_address_candidate(recipient_address, relay_domain)

        try:
            body = cap_body(body)
        except BodyTooLargeError:
            audit_event("worker.message_rejected", reason=REJECT_BODY_OVERSIZE)
            if relay_candidate:
                return
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

        if relay_candidate:
            if not check_rate_limit(
                sender_email, sender_authenticated=sender_authenticated
            ):
                audit_event("worker.message_rejected", reason=REJECT_RATE_LIMIT)
                return
            token = parse_relay_address(recipient_address or "", relay_domain)
            if token is None:
                audit_event("worker.message_rejected", reason="relay_invalid")
                return
            destination = resolve_relay_destination(
                recipient_address=recipient_address or "",
                sender_email=sender_email,
                sender_authenticated=sender_authenticated,
                relay_domain=relay_domain,
            )
            if destination is None:
                audit_event("worker.message_rejected", reason="relay_forbidden")
                return
            send_relay_email(
                to_address=destination,
                proxy_address=build_relay_address(token, relay_domain),
                subject=subject,
                body_text=body,
                source_message=(
                    base64.b64decode(raw_message_b64) if raw_message_b64 else None
                ),
                trace_id=trace_id,
            )
            audit_event("worker.relay_forwarded", outcome="success")
            return

        raw_message = base64.b64decode(raw_message_b64) if raw_message_b64 else None
        verified_body = verify_admin_request(sender_email, subject, raw_message)
        if verified_body is None and is_admin_request_candidate(sender_email, subject):
            audit_event("worker.message_rejected", reason=REJECT_ADMIN_AUTH)
            return
        if verified_body is not None:
            command = extract_command(verified_body)
            body_text = extract_body_text(verified_body)
            reply = await handle_admin_command(command, body_text)
            send_reply(
                to_address=sender_email,
                subject=f"Re: {subject}",
                body_text=reply,
                audience="internal",
                **_trace_kwargs(trace_id),
                **_thread_headers(inbound_message_id, inbound_references),
            )
            return

        if source_mailbox == "primary" and is_primary_intake_paused():
            audit_event("worker.message_rejected", reason="primary_intake_paused")
            return

        if (
            source_mailbox == "primary" or is_proactive
        ) and not check_daily_token_budget(get_settings().daily_agent_token_cap):
            # Belt-and-braces race guard: the producer already skips
            # enqueueing over-budget primary mail (see worker/producer.py) and
            # the three hourly scans check the budget before deferring (see
            # worker/proactive.py and worker/event_scan.py), but a job already
            # sitting in the Procrastinate queue when the cap tripped
            # mid-flight - primary or proactive - could still reach here.
            # Proactive/synthetic jobs have no inbound sender to notify and
            # regenerate on the next scan, so this is a silent drop either way.
            audit_event(
                "worker.message_rejected", reason="daily_token_budget_exhausted"
            )
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

        is_safe, scan_reason = await scan_content(body)
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
        await record_sent_email_memories(consent_result.sent_email_memories)
        email_body = body
        if consent_result.handled:
            # The decision is fully consumed; forward only an authenticated
            # participant's own leftover text (never rejected-path content)
            # so a substantive aside still reaches the agent.
            if not consent_result.remainder or sender_user_id is None:
                return
            email_body = _consent_remainder_body(
                consent_result.remainder, consent_result.outcome
            )
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
            "email_body": email_body,
            "sender_display_name": sender_display_name,
            "attachment_count": attachment_count,
            "is_proactive": is_proactive,
        }
        if proactive_candidate_id:
            agent_kwargs["proactive_candidate_id"] = proactive_candidate_id
        if proactive_event_id:
            agent_kwargs["proactive_event_id"] = proactive_event_id
        if proactive_event_version is not None:
            agent_kwargs["proactive_event_version"] = proactive_event_version
        if trace_id:
            agent_kwargs["trace_id"] = trace_id
        if inbound_message_id:
            agent_kwargs["inbound_message_id"] = inbound_message_id
            agent_kwargs["inbound_references"] = inbound_references
            agent_kwargs["inbound_body_for_quote"] = inbound_body_for_quote or body
            agent_kwargs["inbound_date"] = inbound_date
        if capabilities is not None:
            agent_kwargs["capabilities"] = capabilities
        try:
            await run_agent_for_email(**agent_kwargs)
        except Exception as exc:
            audit_event(
                "worker.agent_failed",
                outcome="error",
                error_type=type(exc).__name__,
            )
            if (
                context is not None
                and context.job.attempts >= _PROCESS_EMAIL_MAX_ATTEMPTS
            ):
                record_job_exhausted()
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
    from thenetwork.security.content_scan import assert_content_scanner_ready
    from thenetwork.worker.metrics import configure_worker_metrics

    validate_embedding_configuration()
    configure_audit_logging()
    configure_worker_metrics()
    assert_sanitizer_ready()
    assert_content_scanner_ready()
    asyncio.run(run_worker())


def producer_main() -> None:
    """Console entrypoint: run a single IMAP poll cycle (for manual/cron use)."""
    from thenetwork.embed.embeddings import validate_embedding_configuration
    from thenetwork.worker.producer import run_producer_cycle

    validate_embedding_configuration()
    configure_audit_logging()
    print(run_producer_cycle())
