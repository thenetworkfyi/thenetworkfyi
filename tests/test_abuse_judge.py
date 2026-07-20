from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlmodel import Session, select

from thenetwork.db.models import (
    PrimaryIntakeJudgeState,
    PrimaryIntakeObservation,
    PrimaryIntakeState,
)
from thenetwork.audit import LOGGER_NAME, audit_event
from thenetwork.worker.abuse_judge import (
    ABUSE_JUDGE_PER_SENDER_LIMIT,
    AbuseJudgment,
    AbuseReason,
    AbuseVerdict,
    _JudgeSnapshot,
    _load_judge_snapshot,
    _opaque_payload,
    _record_judgment,
    _run_abuse_judge,
    judge_primary_email_abuse,
)


def _observation(
    uid: str,
    *,
    observed_at: datetime,
    sender: str,
    domain: str = "domain-fingerprint",
    body: str = "body-fingerprint",
) -> PrimaryIntakeObservation:
    return PrimaryIntakeObservation(
        mailbox_uid=uid,
        trace_id=f"trace-{uid}",
        observed_at=observed_at,
        sender_authenticated=False,
        sender_known=False,
        sender_fingerprint=sender,
        domain_fingerprint=domain,
        body_fingerprint=body,
    )


def _database(monkeypatch, observations=()):
    engine = create_engine("sqlite://")
    PrimaryIntakeState.__table__.create(engine)
    PrimaryIntakeObservation.__table__.create(engine)
    PrimaryIntakeJudgeState.__table__.create(engine)
    with Session(engine) as session:
        session.add(PrimaryIntakeState(key="primary", paused=False))
        session.add(PrimaryIntakeJudgeState(key="primary"))
        session.add_all(observations)
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

    monkeypatch.setattr("thenetwork.worker.abuse_judge.get_session", get_test_session)
    return engine


def test_judgment_rejects_reason_from_another_verdict():
    with pytest.raises(ValidationError):
        AbuseJudgment(
            verdict=AbuseVerdict.NORMAL,
            reason=AbuseReason.MULTI_SENDER_CAMPAIGN,
        )


def test_judge_verdict_and_reason_are_safe_audit_enums(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    audit_event(
        "intake.abuse_judge.completed",
        verdict="coordinated_abuse",
        reason="multi_sender_campaign",
        outcome="blocked",
    )

    payload = json.loads(caplog.records[0].message)
    assert payload["verdict"] == "coordinated_abuse"
    assert payload["reason"] == "multi_sender_campaign"


def test_snapshot_is_bounded_sender_diverse_and_advances_only_for_new_rows(
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    observations = [
        _observation(
            str(index),
            observed_at=now - timedelta(minutes=index),
            sender="repeat-sender",
        )
        for index in range(ABUSE_JUDGE_PER_SENDER_LIMIT + 2)
    ]
    observations.append(_observation("other", observed_at=now, sender="other-sender"))
    engine = _database(monkeypatch, observations)

    snapshot = _load_judge_snapshot(now)

    assert snapshot is not None
    senders = [row.sender_fingerprint for row in snapshot.observations]
    assert senders.count("repeat-sender") == ABUSE_JUDGE_PER_SENDER_LIMIT
    assert "other-sender" in senders

    with Session(engine) as session:
        state = session.get(PrimaryIntakeJudgeState, "primary")
        assert state is not None
        state.last_observed_at = snapshot.cursor_observed_at
        state.last_mailbox_uid = snapshot.cursor_mailbox_uid
        session.add(state)
        session.commit()

    assert _load_judge_snapshot(now) is None


def test_payload_relabels_all_fingerprints_and_contains_no_raw_fields():
    observed_at = datetime.now(timezone.utc)
    snapshot = _JudgeSnapshot(
        observations=(
            _observation(
                "1",
                observed_at=observed_at,
                sender="secret-sender-hmac",
                domain="secret-domain-hmac",
                body="secret-body-hmac",
            ),
        ),
        cursor_observed_at=observed_at,
        cursor_mailbox_uid="1",
    )

    serialized = _opaque_payload(snapshot)
    payload = json.loads(serialized)

    assert "secret" not in serialized
    assert set(payload["observations"][0]) == {
        "observed_at",
        "sender",
        "domain",
        "body",
        "sender_authenticated",
        "sender_known",
    }
    assert payload["observations"][0]["sender"] == "sender_001"
    assert "subject" not in serialized
    assert "email" not in serialized


@pytest.mark.asyncio
async def test_model_judge_uses_small_model_fixed_prompt_and_no_tools(monkeypatch):
    observed_at = datetime.now(timezone.utc)
    snapshot = _JudgeSnapshot(
        observations=(
            _observation("1", observed_at=observed_at, sender="sender-hmac"),
        ),
        cursor_observed_at=observed_at,
        cursor_mailbox_uid="1",
    )
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        async def run(self, payload):
            captured["payload"] = payload
            return SimpleNamespace(
                output=AbuseJudgment(
                    verdict=AbuseVerdict.NORMAL,
                    reason=AbuseReason.ROUTINE_VARIATION,
                )
            )

    settings = SimpleNamespace(
        small_agent_model="provider:small-model",
        small_agent_api_key="role-key",
        model_request_timeout_seconds=12.0,
    )
    resolve_model = Mock(return_value="resolved-small-model")
    monkeypatch.setattr("thenetwork.worker.abuse_judge.Agent", FakeAgent)
    monkeypatch.setattr("thenetwork.worker.abuse_judge.get_settings", lambda: settings)
    monkeypatch.setattr(
        "thenetwork.worker.abuse_judge.model_with_api_key", resolve_model
    )

    judgment = await _run_abuse_judge(snapshot)

    assert judgment.verdict is AbuseVerdict.NORMAL
    resolve_model.assert_called_once_with("provider:small-model", "role-key", 12.0)
    assert captured["kwargs"]["model"] == "resolved-small-model"
    assert captured["kwargs"]["output_type"] is AbuseJudgment
    assert "tools" not in captured["kwargs"]
    assert "secret-sender-hmac" not in captured["payload"]


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (AbuseVerdict.NORMAL, AbuseReason.ROUTINE_VARIATION),
        (AbuseVerdict.SUSPICIOUS, AbuseReason.UNUSUAL_NEW_SENDER_VOLUME),
    ],
)
def test_non_abuse_verdict_records_cursor_without_changing_intake(
    monkeypatch, verdict, reason
):
    now = datetime.now(timezone.utc)
    engine = _database(
        monkeypatch,
        [_observation("1", observed_at=now, sender="sender-hmac")],
    )
    snapshot = _load_judge_snapshot(now)
    assert snapshot is not None

    recorded, transition = _record_judgment(
        snapshot,
        AbuseJudgment(verdict=verdict, reason=reason),
        now=now,
    )

    assert recorded is True
    assert transition is None
    with Session(engine) as session:
        intake = session.get(PrimaryIntakeState, "primary")
        judge_state = session.get(PrimaryIntakeJudgeState, "primary")
    assert intake is not None and intake.paused is False
    assert judge_state is not None and judge_state.last_verdict == verdict.value


@pytest.mark.asyncio
async def test_coordinated_abuse_pauses_once_and_repeat_run_is_idempotent(
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    engine = _database(
        monkeypatch,
        [
            _observation(
                str(index),
                observed_at=now + timedelta(microseconds=index),
                sender=f"sender-{index}",
                body="shared-body",
            )
            for index in range(6)
        ],
    )
    judgment = AbuseJudgment(
        verdict=AbuseVerdict.COORDINATED_ABUSE,
        reason=AbuseReason.MULTI_SENDER_CAMPAIGN,
    )
    run_model = AsyncMock(return_value=judgment)
    notify = Mock()
    audit = Mock()
    monkeypatch.setattr(
        "thenetwork.worker.abuse_judge.get_settings",
        lambda: SimpleNamespace(primary_intake_burst_monitoring_enabled=True),
    )
    monkeypatch.setattr("thenetwork.worker.abuse_judge._run_abuse_judge", run_model)
    monkeypatch.setattr(
        "thenetwork.worker.abuse_judge.notify_primary_intake_transition", notify
    )
    monkeypatch.setattr("thenetwork.worker.abuse_judge.audit_event", audit)

    await judge_primary_email_abuse.func(0)
    await judge_primary_email_abuse.func(1)

    run_model.assert_awaited_once()
    notify.assert_called_once()
    transition = notify.call_args.args[0]
    assert transition.changed is True
    assert transition.status.reason == "coordinated_abuse"
    with Session(engine) as session:
        intake = session.get(PrimaryIntakeState, "primary")
        state = session.get(PrimaryIntakeJudgeState, "primary")
        observations = session.exec(select(PrimaryIntakeObservation)).all()
    assert intake is not None and intake.paused is True
    assert intake.pause_reason == "coordinated_abuse"
    assert state is not None and state.last_verdict == "coordinated_abuse"
    assert len(observations) == 6
    completed = [
        call
        for call in audit.call_args_list
        if call.args[0] == "intake.abuse_judge.completed"
    ]
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_model_failure_is_audited_without_state_change(monkeypatch):
    now = datetime.now(timezone.utc)
    engine = _database(
        monkeypatch,
        [_observation("1", observed_at=now, sender="sender-hmac")],
    )
    audit = Mock()
    monkeypatch.setattr(
        "thenetwork.worker.abuse_judge.get_settings",
        lambda: SimpleNamespace(primary_intake_burst_monitoring_enabled=True),
    )
    monkeypatch.setattr(
        "thenetwork.worker.abuse_judge._run_abuse_judge",
        AsyncMock(side_effect=RuntimeError("provider returned private response")),
    )
    monkeypatch.setattr("thenetwork.worker.abuse_judge.audit_event", audit)

    await judge_primary_email_abuse.func(0)

    audit.assert_called_once_with(
        "intake.abuse_judge.failed", outcome="error", error_type="RuntimeError"
    )
    with Session(engine) as session:
        intake = session.get(PrimaryIntakeState, "primary")
        state = session.get(PrimaryIntakeJudgeState, "primary")
    assert intake is not None and intake.paused is False
    assert state is not None and state.last_observed_at is None


def test_recording_rolls_back_pause_and_cursor_together(monkeypatch):
    now = datetime.now(timezone.utc)
    engine = _database(
        monkeypatch,
        [_observation("1", observed_at=now, sender="sender-hmac")],
    )
    snapshot = _load_judge_snapshot(now)
    assert snapshot is not None

    def fail_after_pause(session, reason, *, now):
        intake = session.get(PrimaryIntakeState, "primary")
        assert intake is not None
        intake.paused = True
        raise RuntimeError("transaction failure")

    monkeypatch.setattr(
        "thenetwork.worker.abuse_judge.set_primary_intake_paused_in_session",
        fail_after_pause,
    )
    with pytest.raises(RuntimeError, match="transaction failure"):
        _record_judgment(
            snapshot,
            AbuseJudgment(
                verdict=AbuseVerdict.COORDINATED_ABUSE,
                reason=AbuseReason.MULTI_SENDER_CAMPAIGN,
            ),
            now=now,
        )

    with Session(engine) as session:
        intake = session.get(PrimaryIntakeState, "primary")
        state = session.get(PrimaryIntakeJudgeState, "primary")
    assert intake is not None and intake.paused is False
    assert state is not None and state.last_observed_at is None
