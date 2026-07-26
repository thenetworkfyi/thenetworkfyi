"""Unit tests for thenetwork.worker.tasks.process_email's own gates.

These cover the in-worker daily-token-budget race guard directly: a job
already sitting in the Procrastinate queue when the cap trips mid-flight,
independent of whichever pre-check (producer poll or proactive scan) deferred
it in the first place.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _empty_session():
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.get.return_value = None
    return session


@pytest.mark.asyncio
async def test_process_email_rejects_primary_mail_when_budget_exhausted():
    from thenetwork.worker.tasks import process_email

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.is_primary_intake_paused", return_value=False),
        patch(
            "thenetwork.worker.tasks.check_daily_token_budget", return_value=False
        ) as check_budget,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as run_agent,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
    ):
        await process_email.func(
            sender_email="sender@example.com",
            subject="subject",
            body="body",
            sender_authenticated=True,
            source_mailbox="primary",
        )

    check_budget.assert_called_once()
    run_agent.assert_not_awaited()
    send_reply.assert_not_called()


@pytest.mark.asyncio
async def test_process_email_rejects_in_flight_proactive_job_when_budget_exhausted():
    """A proactive job already dequeued when the cap trips mid-flight is
    rejected too, not just primary mail - this is the gap the three hourly
    scans' own pre-check cannot cover for a job already sitting in the
    queue."""
    from thenetwork.worker.tasks import process_email

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch(
            "thenetwork.worker.tasks.check_daily_token_budget", return_value=False
        ) as check_budget,
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as run_agent,
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
    ):
        await process_email.func(
            sender_email="proactive-target@example.com",
            subject="[Proactive] Possible connection",
            body="[System trigger] ...",
            sender_authenticated=True,
            is_proactive=True,
            proactive_candidate_id="other-person",
        )

    check_budget.assert_called_once()
    run_agent.assert_not_awaited()
    # Proactive/synthetic jobs have no inbound sender to notify - a silent
    # drop, same as the primary in-flight case.
    send_reply.assert_not_called()


@pytest.mark.asyncio
async def test_process_email_does_not_check_budget_for_ordinary_non_primary_calls():
    """Guards against over-widening: a plain call with neither source_mailbox
    nor is_proactive set (the shape most other unit tests use) must not
    suddenly start hitting the budget check."""
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.worker.tasks import process_email

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_daily_token_budget") as check_budget,
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=ConsentReplyResult(handled=False),
        ),
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as run_agent,
    ):
        await process_email.func(
            sender_email="sender@example.com",
            subject="subject",
            body="body",
            sender_authenticated=True,
        )

    check_budget.assert_not_called()
    run_agent.assert_awaited_once()
    assert run_agent.await_args.kwargs["attachment_count"] == 0


@pytest.mark.asyncio
async def test_process_email_forwards_attachment_count_to_agent():
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.worker.tasks import process_email

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=ConsentReplyResult(handled=False),
        ),
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as run_agent,
    ):
        await process_email.func(
            sender_email="sender@example.com",
            subject="subject",
            body="body",
            sender_authenticated=True,
            attachment_count=2,
        )

    assert run_agent.await_args.kwargs["attachment_count"] == 2
