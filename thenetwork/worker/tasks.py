"""Procrastinate task definitions for the email processing worker.

The worker is Postgres-native (LISTEN/NOTIFY + SKIP LOCKED, no Redis/broker).
Retries and backoff are Procrastinate's responsibility — no hand-rolled loops.
"""
from __future__ import annotations

import procrastinate

from thenetwork.agent.core import run_agent_for_email
from thenetwork.db.session import get_session
from thenetwork.db.models import Person
from thenetwork.security.content_scan import scan_content
from thenetwork.security.rate_limit import check_rate_limit
from thenetwork.settings import get_settings
from sqlmodel import select

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
    if not check_rate_limit(sender_email):
        return

    is_safe, _ = scan_content(body)
    if not is_safe:
        return

    sender_user_id: str | None = None
    with get_session() as session:
        profile = session.exec(
            select(Person).where(Person.email == sender_email)
        ).first()
        if profile:
            sender_user_id = profile.id

    await run_agent_for_email(
        sender_email=sender_email,
        sender_user_id=sender_user_id,
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

    asyncio.run(run_worker())


def producer_main() -> None:
    """Console entrypoint: run a single IMAP poll cycle (for manual/cron use)."""
    from thenetwork.worker.producer import run_producer_cycle

    print(run_producer_cycle())
