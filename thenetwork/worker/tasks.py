"""Procrastinate task definitions for the email processing worker.

The worker is Postgres-native (LISTEN/NOTIFY + SKIP LOCKED, no Redis/broker).
Retries and backoff are Procrastinate's responsibility — no hand-rolled loops.
"""
from __future__ import annotations

import procrastinate

from thenetwork.agent.core import run_agent_for_email
from thenetwork.db.session import get_session
from thenetwork.db.models import Profile
from thenetwork.security.content_scan import scan_content
from thenetwork.security.rate_limit import check_rate_limit
from sqlmodel import select

# App instance; connector is configured at startup via the database URL
app = procrastinate.App(
    connector=procrastinate.SyncPsycopgConnector(),
    import_paths=["thenetwork.worker.tasks"],
)


@app.task(retry=procrastinate.RetryStrategy(max_attempts=3, wait=60))
async def process_email(
    sender_email: str,
    subject: str,
    body: str,
) -> None:
    """Procrastinate worker task: run the agent for one inbound email.

    Checks rate limit and optional content scan before handing off.
    Agent runs with sender's existing user_id (None if first contact).
    """
    # Rate limit check — drop over-quota senders without agent invocation
    if not check_rate_limit(sender_email):
        return

    # Optional content scan (defense-in-depth, not primary defense)
    is_safe, reason = scan_content(body)
    if not is_safe:
        return

    # Look up existing profile for this sender
    sender_user_id: str | None = None
    with get_session() as session:
        profile = session.exec(
            select(Profile).where(Profile.email == sender_email)
        ).first()
        if profile:
            sender_user_id = profile.id

    await run_agent_for_email(
        sender_email=sender_email,
        sender_user_id=sender_user_id,
        email_subject=subject,
        email_body=body,
    )
