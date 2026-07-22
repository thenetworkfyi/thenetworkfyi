"""Durable server-side controls for pausing primary email intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from thenetwork.audit import audit_event
from thenetwork.db.models import PrimaryIntakeState
from thenetwork.db.session import get_session
from thenetwork.worker.metrics import (
    ControlAction,
    ControlActor,
    ControlReason,
    record_control_action,
)

PRIMARY_INTAKE_KEY = "primary"


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
    return _set_primary_intake_state(paused=True, reason=reason)


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


def _set_primary_intake_state(
    *, paused: bool, reason: PrimaryIntakePauseReason | None
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
            state.pause_reason = reason.value if reason is not None else None
            state.paused_at = now if paused else None
            state.updated_at = now

        status = PrimaryIntakeStatus(
            paused=state.paused,
            reason=state.pause_reason,
            paused_at=state.paused_at,
        )

    if changed:
        record_control_action(
            action=ControlAction.PAUSE if paused else ControlAction.RESUME,
            actor=(
                ControlActor.ADMIN
                if reason in {None, PrimaryIntakePauseReason.ADMIN}
                else ControlActor.SYSTEM
            ),
            reason=ControlReason(reason.value if reason is not None else "admin"),
        )
    audit_event(
        "database.action",
        action="pause" if paused else "resume",
        record_type="primary_intake",
        outcome="success" if changed else "exists",
    )
    return PrimaryIntakeTransition(status=status, changed=changed)
