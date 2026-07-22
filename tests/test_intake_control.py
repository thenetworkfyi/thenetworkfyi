from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from thenetwork.db.models import PrimaryIntakeState
from thenetwork.email.intake_control import (
    PrimaryIntakePauseReason,
    get_primary_intake_status,
    pause_primary_intake,
    resume_primary_intake,
)
from thenetwork.worker.metrics import ControlAction, ControlActor, ControlReason


def _session_context(session):
    @contextmanager
    def context():
        yield session

    return context()


def test_missing_intake_state_defaults_to_active():
    session = MagicMock()
    session.get.return_value = None

    with patch(
        "thenetwork.email.intake_control.get_session",
        return_value=_session_context(session),
    ):
        status = get_primary_intake_status()

    assert status.paused is False
    assert status.reason is None


def test_pause_and_resume_notify_once_per_state_transition():
    state = PrimaryIntakeState(
        key="primary",
        paused=False,
        updated_at=datetime.now(timezone.utc),
    )
    session = MagicMock()
    session.get.return_value = state
    settings = SimpleNamespace(admin_emails=["admin@example.com"])

    with (
        patch(
            "thenetwork.email.intake_control.get_session",
            side_effect=lambda: _session_context(session),
        ),
        patch("thenetwork.email.intake_control.get_settings", return_value=settings),
        patch("thenetwork.email.intake_control.notify_admins") as notify_admins,
        patch(
            "thenetwork.email.intake_control.record_control_action"
        ) as record_control,
    ):
        first_pause = pause_primary_intake(PrimaryIntakePauseReason.ADMIN)
        repeated_pause = pause_primary_intake(PrimaryIntakePauseReason.ADMIN)
        resumed = resume_primary_intake()
        repeated_resume = resume_primary_intake()

    assert first_pause.changed is True
    assert repeated_pause.changed is False
    assert resumed.changed is True
    assert repeated_resume.changed is False
    assert notify_admins.call_count == 2
    assert record_control.call_args_list == [
        call(
            action=ControlAction.PAUSE,
            actor=ControlActor.ADMIN,
            reason=ControlReason.ADMIN,
        ),
        call(
            action=ControlAction.RESUME,
            actor=ControlActor.ADMIN,
            reason=ControlReason.ADMIN,
        ),
    ]
    pause_notice, resume_notice = notify_admins.call_args_list
    assert pause_notice.args[0] is settings
    assert pause_notice.args[1:] == (
        "[The Network] Primary intake paused",
        "Primary email intake has been paused. Ordinary primary messages will remain "
        "unread until a PGP-authenticated administrator resumes intake. Relay delivery "
        "continues separately.",
    )
    assert resume_notice.args[1:] == (
        "[The Network] Primary intake resumed",
        "Primary email intake has been resumed. Unread primary messages are eligible "
        "for processing again.",
    )


def test_pause_reason_is_closed_enum_and_persisted_without_pii():
    state = PrimaryIntakeState(key="primary", paused=False)
    session = MagicMock()
    session.get.return_value = state

    with (
        patch(
            "thenetwork.email.intake_control.get_session",
            return_value=_session_context(session),
        ),
        patch("thenetwork.email.intake_control.get_settings") as settings,
        patch("thenetwork.email.intake_control.notify_admins"),
    ):
        settings.return_value.admin_emails = []
        transition = pause_primary_intake(PrimaryIntakePauseReason.NEW_SENDER_BURST)

    assert transition.status.reason == "new_sender_burst"
    assert state.pause_reason == "new_sender_burst"
    assert state.paused_at is not None
