from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlmodel import Session, select

from thenetwork.db.models import (
    Person,
    PrimaryIntakeObservation,
    PrimaryIntakeState,
)
from thenetwork.email.inbound import InboundMessage
from thenetwork.email.intake_observations import observe_primary_intake_batch


def _message(index: int, *, sender: str | None = None, authenticated=False):
    return InboundMessage(
        uid=str(index),
        sender=sender or f"sender-{index}@example.com",
        subject=f"Private subject {index}",
        body=f"Private campaign body {index}",
        auto_submitted=None,
        sender_authenticated=authenticated,
        trace_id=str(uuid4()),
    )


def _database(monkeypatch):
    engine = create_engine("sqlite://")
    Person.__table__.create(engine)
    PrimaryIntakeState.__table__.create(engine)
    PrimaryIntakeObservation.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            PrimaryIntakeState(
                key="primary",
                paused=False,
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
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

    monkeypatch.setattr(
        "thenetwork.email.intake_observations.get_session", get_test_session
    )
    monkeypatch.setattr(
        "thenetwork.email.intake_observations.notify_primary_intake_transition",
        lambda _transition: None,
    )
    return engine


def test_twenty_five_distinct_unregistered_senders_atomically_pause(monkeypatch):
    engine = _database(monkeypatch)
    messages = [_message(index) for index in range(25)]

    result = observe_primary_intake_batch(messages, secret="monitor-secret")

    assert result.paused is True
    assert result.newly_observed == 25
    assert result.distinct_new_senders == 25
    with Session(engine) as session:
        state = session.get(PrimaryIntakeState, "primary")
        rows = session.exec(select(PrimaryIntakeObservation)).all()
    assert state is not None
    assert state.paused is True
    assert state.pause_reason == "new_sender_burst"
    assert len(rows) == 25

    stored = "\n".join(
        str(
            (
                row.mailbox_uid,
                row.trace_id,
                row.sender_fingerprint,
                row.domain_fingerprint,
                row.body_fingerprint,
            )
        )
        for row in rows
    )
    for message in messages:
        assert message.sender not in stored
        assert message.subject not in stored
        assert message.body not in stored


def test_repeated_poll_is_idempotent(monkeypatch):
    engine = _database(monkeypatch)
    messages = [_message(index) for index in range(3)]

    first = observe_primary_intake_batch(messages, secret="monitor-secret")
    second = observe_primary_intake_batch(messages, secret="monitor-secret")

    assert first.newly_observed == 3
    assert second.newly_observed == 0
    assert second.paused is False
    with Session(engine) as session:
        assert len(session.exec(select(PrimaryIntakeObservation)).all()) == 3


def test_distinct_sender_threshold_ignores_repeats_and_registered_people(monkeypatch):
    engine = _database(monkeypatch)
    with Session(engine) as session:
        session.add(Person(email="known@example.com", name="Known"))
        session.commit()
    messages = [_message(index, sender="repeat@example.com") for index in range(30)]
    messages.append(_message(31, sender="known@example.com", authenticated=True))

    result = observe_primary_intake_batch(messages, secret="monitor-secret")

    assert result.paused is False
    assert result.distinct_new_senders == 1
    with Session(engine) as session:
        known = session.exec(
            select(PrimaryIntakeObservation).where(
                PrimaryIntakeObservation.mailbox_uid == "31"
            )
        ).one()
    assert known.sender_authenticated is True
    assert known.sender_known is True


def test_resume_timestamp_is_a_fresh_monitoring_baseline(monkeypatch):
    engine = _database(monkeypatch)
    old_messages = [_message(index) for index in range(24)]
    observe_primary_intake_batch(old_messages, secret="monitor-secret")
    with Session(engine) as session:
        state = session.get(PrimaryIntakeState, "primary")
        assert state is not None
        state.updated_at = datetime.now(timezone.utc)
        session.add(state)
        session.commit()

    result = observe_primary_intake_batch([_message(30)], secret="monitor-secret")

    assert result.paused is False
    assert result.distinct_new_senders == 1
