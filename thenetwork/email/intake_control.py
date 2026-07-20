"""Durable server-side controls for pausing primary email intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from thenetwork.audit import audit_event
from thenetwork.db.models import PrimaryIntakeState
from thenetwork.db.session import get_session
from thenetwork.email.outbound import notify_admins
from thenetwork.settings import get_settings

PRIMARY_INTAKE_KEY = "primary"

_PAUSED_SUBJECT = "[The Network] Primary intake paused"
_PAUSED_BODY = (
    "Primary email intake has been paused. Ordinary primary messages will remain "
    "unread until a PGP-authenticated administrator resumes intake. Relay delivery "
    "continues separately."
)
_RESUMED_SUBJECT = "[The Network] Primary intake resumed"
_RESUMED_BODY = (
    "Primary email intake has been resumed. Unread primary messages are eligible "
    "for processing again."
)


class PrimaryIntakePauseReason(StrEnum):
    ADMIN = "admin"
    NEW_SENDER_BURST = "new_sender_burst"
    COORDINATED_ABUSE = "coordinated_abuse"


@dataclass(frozen=True, slots=True)
class PrimaryIntakeStatus:
    paused: bool
    reason: str | None = None
    paused_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PrimaryIntakeTransition:
    status: PrimaryIntakeStatus
    changed: bool


def get_primary_intake_status() -> PrimaryIntakeStatus:
    with get_session() as session:
        state = session.get(PrimaryIntakeState, PRIMARY_INTAKE_KEY)
        if state is None:
            return PrimaryIntakeStatus(paused=False)
        return PrimaryIntakeStatus(
            paused=state.paused,
            reason=state.pause_reason,
            paused_at=state.paused_at,
        )


def is_primary_intake_paused() -> bool:
    return get_primary_intake_status().paused


def pause_primary_intake(
    reason: PrimaryIntakePauseReason,
) -> PrimaryIntakeTransition:
    return _set_primary_intake_state(paused=True, reason=reason.value)


def resume_primary_intake() -> PrimaryIntakeTransition:
    return _set_primary_intake_state(paused=False, reason=None)


def set_primary_intake_paused_in_session(
    session,
    reason: PrimaryIntakePauseReason,
    *,
    now: datetime,
) -> PrimaryIntakeTransition:
    """Pause while participating in the caller's database transaction."""
    state = session.get(
        PrimaryIntakeState,
        PRIMARY_INTAKE_KEY,
        with_for_update=True,
    )
    if state is None:
        state = PrimaryIntakeState(key=PRIMARY_INTAKE_KEY)
        session.add(state)
    changed = not state.paused
    if changed:
        state.paused = True
        state.pause_reason = reason.value
        state.paused_at = now
        state.updated_at = now
    return PrimaryIntakeTransition(
        status=PrimaryIntakeStatus(
            paused=state.paused,
            reason=state.pause_reason,
            paused_at=state.paused_at,
        ),
        changed=changed,
    )


def notify_primary_intake_transition(transition: PrimaryIntakeTransition) -> None:
    if not transition.changed:
        return
    settings = get_settings()
    if transition.status.paused:
        notify_admins(settings, _PAUSED_SUBJECT, _PAUSED_BODY)
    else:
        notify_admins(settings, _RESUMED_SUBJECT, _RESUMED_BODY)


def _set_primary_intake_state(
    *, paused: bool, reason: str | None
) -> PrimaryIntakeTransition:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        state = session.get(
            PrimaryIntakeState,
            PRIMARY_INTAKE_KEY,
            with_for_update=True,
        )
        if state is None:
            state = PrimaryIntakeState(key=PRIMARY_INTAKE_KEY)
            session.add(state)
        changed = state.paused != paused
        if changed:
            state.paused = paused
            state.pause_reason = reason
            state.paused_at = now if paused else None
            state.updated_at = now

        status = PrimaryIntakeStatus(
            paused=state.paused,
            reason=state.pause_reason,
            paused_at=state.paused_at,
        )

    audit_event(
        "database.action",
        action="pause" if paused else "resume",
        record_type="primary_intake",
        outcome="success" if changed else "exists",
    )
    transition = PrimaryIntakeTransition(status=status, changed=changed)
    notify_primary_intake_transition(transition)
    return transition
