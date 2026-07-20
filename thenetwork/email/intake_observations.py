"""PII-safe, pre-enqueue primary inbox burst detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func
from sqlmodel import select

from thenetwork.audit import audit_event
from thenetwork.db.models import (
    Person,
    PrimaryIntakeObservation,
    PrimaryIntakeState,
)
from thenetwork.db.session import get_session
from thenetwork.email.inbound import InboundMessage
from thenetwork.email.intake_control import (
    PRIMARY_INTAKE_KEY,
    PrimaryIntakePauseReason,
    PrimaryIntakeTransition,
    notify_primary_intake_transition,
    set_primary_intake_paused_in_session,
)
from thenetwork.security.intake_fingerprint import intake_fingerprints
from thenetwork.security.sender_identifier import normalize_sender_identifier_identity

NEW_SENDER_BURST_THRESHOLD = 25
NEW_SENDER_BURST_WINDOW = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class BurstObservationResult:
    paused: bool
    newly_observed: int
    distinct_new_senders: int


def observe_primary_intake_batch(
    messages: list[InboundMessage], *, secret: str
) -> BurstObservationResult:
    """Persist one batch and atomically pause when its new-sender count trips."""
    now = datetime.now(timezone.utc)
    transition: PrimaryIntakeTransition | None = None

    with get_session() as session:
        state = session.get(
            PrimaryIntakeState,
            PRIMARY_INTAKE_KEY,
            with_for_update=True,
        )
        if state is not None and state.paused:
            return BurstObservationResult(True, 0, 0)

        unique_messages = {message.uid: message for message in messages}
        uids = list(unique_messages)
        existing_uids = set()
        if uids:
            existing_uids = set(
                session.exec(
                    select(PrimaryIntakeObservation.mailbox_uid).where(
                        PrimaryIntakeObservation.mailbox_uid.in_(uids)
                    )
                ).all()
            )

        candidates = [
            message
            for uid, message in unique_messages.items()
            if uid not in existing_uids
        ]
        authenticated_senders = {
            normalize_sender_identifier_identity(message.sender)
            for message in candidates
            if message.sender_authenticated
        }
        known_senders = set()
        if authenticated_senders:
            known_senders = set(
                session.exec(
                    select(Person.email).where(Person.email.in_(authenticated_senders))
                ).all()
            )

        for message in candidates:
            normalized_sender = normalize_sender_identifier_identity(message.sender)
            sender_fingerprint, domain_fingerprint, body_fingerprint = (
                intake_fingerprints(message.sender, message.body, secret=secret)
            )
            session.add(
                PrimaryIntakeObservation(
                    mailbox_uid=message.uid,
                    trace_id=message.trace_id,
                    observed_at=now,
                    sender_authenticated=message.sender_authenticated,
                    sender_known=(
                        message.sender_authenticated
                        and normalized_sender in known_senders
                    ),
                    sender_fingerprint=sender_fingerprint,
                    domain_fingerprint=domain_fingerprint,
                    body_fingerprint=body_fingerprint,
                )
            )

        newly_observed = len(candidates)
        if newly_observed:
            session.flush()
            rolling_cutoff = now - NEW_SENDER_BURST_WINDOW
            if state is not None and not state.paused:
                state_updated_at = state.updated_at
                if state_updated_at.tzinfo is None:
                    state_updated_at = state_updated_at.replace(tzinfo=timezone.utc)
                rolling_cutoff = max(rolling_cutoff, state_updated_at)
            distinct_new_senders = int(
                session.exec(
                    select(
                        func.count(
                            distinct(PrimaryIntakeObservation.sender_fingerprint)
                        )
                    )
                    .where(PrimaryIntakeObservation.sender_known.is_(False))
                    .where(PrimaryIntakeObservation.observed_at >= rolling_cutoff)
                ).one()
            )
        else:
            distinct_new_senders = 0

        if distinct_new_senders >= NEW_SENDER_BURST_THRESHOLD:
            transition = set_primary_intake_paused_in_session(
                session,
                PrimaryIntakePauseReason.NEW_SENDER_BURST,
                now=now,
            )

    if transition is not None:
        audit_event(
            "database.action",
            action="pause",
            record_type="primary_intake",
            outcome="success" if transition.changed else "exists",
        )
        notify_primary_intake_transition(transition)
    return BurstObservationResult(
        paused=transition is not None and transition.status.paused,
        newly_observed=newly_observed,
        distinct_new_senders=distinct_new_senders,
    )
