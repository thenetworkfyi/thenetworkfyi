from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select

from thenetwork.admin.commands import handle_admin_command
from thenetwork.db.models import (
    Person,
    PrimaryIntakeJudgeState,
    PrimaryIntakeObservation,
    PrimaryIntakeState,
)
from thenetwork.email.inbound import InboundMessage
from thenetwork.worker.abuse_judge import (
    AbuseJudgment,
    AbuseReason,
    AbuseVerdict,
    judge_primary_email_abuse,
)
from thenetwork.worker.producer import _poll_mailbox_and_enqueue


def _message(uid: str, sender: str, *, recipient: str | None = None):
    return InboundMessage(
        uid=uid,
        sender=sender,
        subject="Private inbound subject",
        body="Repeated private campaign body",
        auto_submitted=None,
        sender_authenticated=True,
        recipient_address=recipient,
        trace_id=str(uuid4()),
    )


def _database(monkeypatch):
    engine = create_engine("sqlite://")
    Person.__table__.create(engine)
    PrimaryIntakeState.__table__.create(engine)
    PrimaryIntakeObservation.__table__.create(engine)
    PrimaryIntakeJudgeState.__table__.create(engine)
    with Session(engine) as session:
        session.add(PrimaryIntakeState(key="primary", paused=False))
        session.add(PrimaryIntakeJudgeState(key="primary"))
        session.commit()

    @contextmanager
    def get_test_session():
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    for module in (
        "thenetwork.email.intake_control",
        "thenetwork.email.intake_observations",
        "thenetwork.worker.abuse_judge",
    ):
        monkeypatch.setattr(f"{module}.get_session", get_test_session)
    return engine


@pytest.mark.asyncio
async def test_campaign_pause_review_resume_and_relay_continuity(monkeypatch):
    engine = _database(monkeypatch)
    settings = SimpleNamespace(
        primary_intake_burst_monitoring_enabled=True,
        sender_identifier_secret="monitor-secret",
        relay_domain="relay.example.com",
    )
    monkeypatch.setattr("thenetwork.worker.producer.get_settings", lambda: settings)
    monkeypatch.setattr("thenetwork.worker.abuse_judge.get_settings", lambda: settings)
    monkeypatch.setattr(
        "thenetwork.email.intake_control.get_settings", lambda: settings
    )
    monkeypatch.setattr(
        "thenetwork.worker.producer.is_disposable", lambda _email: False
    )

    campaign = [
        _message(str(index), f"campaign-{index}@example.com") for index in range(25)
    ]
    pause_notifications = Mock()
    campaign_jobs = Mock()
    campaign_seen = Mock()
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=campaign),
        patch("thenetwork.worker.producer.process_email", campaign_jobs),
        patch("thenetwork.worker.producer.mark_messages_seen", campaign_seen),
        patch("thenetwork.email.intake_control.notify_admins", pause_notifications),
    ):
        assert _poll_mailbox_and_enqueue("primary") == 0

        monkeypatch.setattr(
            "thenetwork.worker.abuse_judge._run_abuse_judge",
            AsyncMock(
                return_value=AbuseJudgment(
                    verdict=AbuseVerdict.COORDINATED_ABUSE,
                    reason=AbuseReason.MULTI_SENDER_CAMPAIGN,
                )
            ),
        )
        await judge_primary_email_abuse.func(0)

    campaign_jobs.defer.assert_not_called()
    campaign_seen.assert_called_once_with([], mailbox="primary")
    assert pause_notifications.call_count == 1
    assert pause_notifications.call_args.args[1] == (
        "[The Network] Primary intake paused"
    )

    with Session(engine) as session:
        intake = session.get(PrimaryIntakeState, "primary")
        judge_state = session.get(PrimaryIntakeJudgeState, "primary")
        observations = session.exec(select(PrimaryIntakeObservation)).all()
    assert intake is not None and intake.paused is True
    assert judge_state is not None
    assert judge_state.last_verdict == "coordinated_abuse"
    assert len(observations) == 25

    relay = _message(
        "relay-1",
        "member@example.com",
        recipient="hidden-token@relay.example.com",
    )
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[relay]),
        patch("thenetwork.worker.producer.process_email") as relay_job,
        patch("thenetwork.worker.producer.mark_messages_seen") as relay_seen,
    ):
        assert _poll_mailbox_and_enqueue("relay", primary_paused=True) == 1
    relay_job.defer.assert_called_once()
    assert relay_job.defer.call_args.kwargs["source_mailbox"] == "relay"
    relay_seen.assert_called_once_with(["relay-1"], mailbox="relay")

    assert await handle_admin_command("intake-status", "") == (
        "Primary intake: paused\nReason: new_sender_burst\nPaused at: "
        f"{intake.paused_at.isoformat()}"
    )
    resume_notifications = Mock()
    with patch("thenetwork.email.intake_control.notify_admins", resume_notifications):
        assert "resumed" in await handle_admin_command("resume-intake", "")
    assert resume_notifications.call_count == 1

    ordinary = _message("ordinary-1", "ordinary@example.com")
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[ordinary]),
        patch("thenetwork.worker.producer.process_email") as ordinary_job,
        patch("thenetwork.worker.producer.mark_messages_seen") as ordinary_seen,
    ):
        assert _poll_mailbox_and_enqueue("primary") == 1
    ordinary_job.defer.assert_called_once()
    assert ordinary_job.defer.call_args.kwargs["source_mailbox"] == "primary"
    ordinary_seen.assert_called_once_with(["ordinary-1"], mailbox="primary")
