from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.tools import (
    EVENT_RECOMMENDATION_STOP_NOTICE,
    FIRST_EVENT_RECOMMENDATION_NOTICE,
    cancel_event,
    create_event,
    resume_event_recommendations,
    send_event_recommendation,
    stop_event_recommendations,
    update_event,
)
from thenetwork.db.models import Event, EventRecommendation, EventSuppression
from thenetwork.settings import Settings


def _session_context(session):
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return lambda: session


def _ctx(
    session,
    *,
    person_id="person-1",
    authenticated=True,
    event_id=None,
    event_version=1,
):
    return SimpleNamespace(
        deps=AgentDeps(
            settings=Settings(
                agent_model="test:model",
                small_agent_model="test:model",
                embed_model="test:embed",
                dispatch_recipient_daily_cap=99,
            ),
            sender_email="owner@example.com",
            sender_user_id=person_id,
            sender_authenticated=authenticated,
            is_proactive=event_id is not None,
            proactive_event_id=event_id,
            proactive_event_version=(event_version if event_id is not None else None),
            session_factory=_session_context(session),
        )
    )


def _active_event(**overrides):
    values = {
        "id": "event-1",
        "submitter_id": "owner-1",
        "text": "Raw event hosted by Alice alice@example.com",
        "gist": "Event hosted by [name] [email]",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=2),
    }
    values.update(overrides)
    return Event(**values)


@pytest.mark.asyncio
async def test_create_event_requires_authenticated_registered_sender():
    session = MagicMock()
    ctx = _ctx(session, person_id=None, authenticated=False)

    with patch(
        "thenetwork.agent.tools.sanitize_text_high_fidelity", new_callable=AsyncMock
    ) as sanitize:
        result = await create_event(
            ctx,
            text="event",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

    assert result == {"status": "error", "reason": "sender_not_authenticated"}
    sanitize.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_event_expiry_is_a_structured_error_not_validation_retry():
    session = MagicMock()
    ctx = _ctx(session, person_id="owner-1")

    result = await create_event(ctx, text="event", expires_at="not-a-timestamp")

    assert result == {"status": "error", "reason": "invalid_event_expiry"}
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_event_persists_raw_privately_and_returns_only_sealed_projection():
    session = MagicMock()
    ctx = _ctx(session, person_id="owner-1")
    raw = "Alice hosts a compiler meetup; alice@example.com"
    gist = "[name] hosts a compiler meetup; [email]"

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text_high_fidelity",
            new=AsyncMock(return_value=gist),
        ) as sanitize,
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.25] * 1536),
        ) as embed,
    ):
        result = await create_event(
            ctx,
            text=raw,
            recurrence="monthly with Alice at alice@example.com",
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )

    stored = session.add.call_args.args[0]
    assert isinstance(stored, Event)
    assert stored.submitter_id == "owner-1"
    assert stored.text == raw
    assert stored.gist == gist
    assert stored.embedding == [0.25] * 1536
    sanitize.assert_awaited_once_with(
        f"{raw}\nRecurrence: monthly with Alice at alice@example.com"
    )
    embed.assert_awaited_once_with(gist)
    assert result["status"] == "created"
    assert result["gist"] == gist
    assert "text" not in result
    assert "submitter_id" not in result
    assert "recurrence" not in result


@pytest.mark.asyncio
async def test_update_and_cancel_reject_non_owner_without_mutation():
    event = _active_event(submitter_id="owner-2")
    session = MagicMock()
    session.get.return_value = event
    ctx = _ctx(session, person_id="owner-1")

    with patch(
        "thenetwork.agent.tools.sanitize_text_high_fidelity", new_callable=AsyncMock
    ) as sanitize:
        update_result = await update_event(
            ctx,
            event_id=event.id,
            text="replacement",
            expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        cancel_result = await cancel_event(ctx, event.id)

    assert update_result == {"status": "forbidden", "reason": "not_event_owner"}
    assert cancel_result == {"status": "forbidden", "reason": "not_event_owner"}
    sanitize.assert_not_awaited()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_owner_update_preserves_event_id_and_rebuilds_sealed_embedding():
    event = _active_event(submitter_id="owner-1")
    session = MagicMock()
    session.get.return_value = event
    ctx = _ctx(session, person_id="owner-1")
    expiry = datetime.now(timezone.utc) + timedelta(days=10)

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text_high_fidelity",
            new=AsyncMock(return_value="sealed replacement"),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=[0.5] * 1536),
        ),
    ):
        result = await update_event(
            ctx,
            event_id="event-1",
            text="raw replacement",
            expires_at=expiry,
            recurrence="weekly",
        )

    assert event.id == "event-1"
    assert event.text == "raw replacement"
    assert event.gist == "sealed replacement"
    assert event.recurrence == "weekly"
    assert event.version == 2
    assert result["event_id"] == "event-1"
    assert "text" not in result and "submitter_id" not in result
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_stop_and_resume_touch_only_event_suppression_state():
    session = MagicMock()
    session.get.side_effect = [None, EventSuppression(person_id="person-1")]
    ctx = _ctx(session)

    stopped = await stop_event_recommendations(ctx)
    resumed = await resume_event_recommendations(ctx)

    assert stopped == {"status": "suppressed"}
    assert resumed == {"status": "resumed"}
    added = session.add.call_args.args[0]
    deleted = session.delete.call_args.args[0]
    assert isinstance(added, EventSuppression)
    assert isinstance(deleted, EventSuppression)
    assert all(call.args[0] is EventSuppression for call in session.get.call_args_list)


@pytest.mark.asyncio
async def test_stop_and_resume_require_authenticated_current_sender():
    session = MagicMock()
    ctx = _ctx(session, authenticated=False)

    stopped = await stop_event_recommendations(ctx)
    resumed = await resume_event_recommendations(ctx)

    assert stopped == {"status": "error", "reason": "sender_not_authenticated"}
    assert resumed == {"status": "error", "reason": "sender_not_authenticated"}
    session.get.assert_not_called()


def _delivery_session(event, recommendation, *, suppressed=False, prior=0):
    session = MagicMock()

    def get(model, key):
        if model is Event:
            return event
        if model is EventSuppression:
            return EventSuppression(person_id=key) if suppressed else None
        raise AssertionError(f"unexpected model lookup: {model}")

    session.get.side_effect = get
    result = MagicMock()
    result.first.return_value = recommendation
    result.one.return_value = prior
    session.exec.return_value = result
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "recommendation", "suppressed", "reason"),
    [
        (
            _active_event(cancelled_at=datetime.now(timezone.utc)),
            MagicMock(notified_at=None),
            False,
            "event_cancelled",
        ),
        (
            _active_event(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
            MagicMock(notified_at=None),
            False,
            "event_expired",
        ),
        (
            _active_event(),
            MagicMock(notified_at=None),
            True,
            "event_recommendations_stopped",
        ),
        (
            _active_event(),
            MagicMock(notified_at=datetime.now(timezone.utc)),
            False,
            "event_already_notified",
        ),
        (
            _active_event(submitter_id="person-1"),
            MagicMock(notified_at=None),
            False,
            "self_event",
        ),
    ],
)
async def test_event_send_hard_gates_before_delivery(
    event, recommendation, suppressed, reason
):
    session = _delivery_session(event, recommendation, suppressed=suppressed)
    ctx = _ctx(session, event_id="event-1")

    with patch(
        "thenetwork.agent.tools._send_event_fyi", new_callable=AsyncMock
    ) as send:
        result = await send_event_recommendation(ctx, event_id="event-1")

    assert result == {"status": "suppressed", "reason": reason}
    send.assert_not_awaited()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_event_send_requires_scan_ledger_and_bound_opaque_event_id():
    event = _active_event()
    session = _delivery_session(event, None)
    ctx = _ctx(session, event_id="event-1")

    wrong = await send_event_recommendation(ctx, event_id="event-2")
    unconsidered = await send_event_recommendation(ctx, event_id="event-1")

    assert wrong == {"status": "forbidden", "reason": "outside_event_trigger"}
    assert unconsidered == {"status": "forbidden", "reason": "event_not_considered"}
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_event_send_rejects_a_trigger_for_an_older_event_version():
    event = _active_event(version=2, gist="unrelated replacement event")
    recommendation = EventRecommendation(
        event_id="event-1", person_id="person-1", event_version=1
    )
    session = _delivery_session(event, recommendation)
    ctx = _ctx(session, event_id="event-1", event_version=1)

    with patch(
        "thenetwork.agent.tools._send_event_fyi", new_callable=AsyncMock
    ) as send:
        result = await send_event_recommendation(ctx, event_id="event-1")

    assert result == {"status": "suppressed", "reason": "event_version_changed"}
    send.assert_not_awaited()
    assert recommendation.notified_at is None
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_event_send_records_notified_only_after_successful_one_way_fyi():
    event = _active_event()
    recommendation = EventRecommendation(event_id="event-1", person_id="person-1")
    session = _delivery_session(event, recommendation, prior=0)
    ctx = _ctx(session, event_id="event-1")

    with patch(
        "thenetwork.agent.tools._send_event_fyi",
        new=AsyncMock(return_value={"status": "sent"}),
    ) as send:
        result = await send_event_recommendation(ctx, event_id="event-1")

    assert result == {"status": "sent"}
    assert recommendation.notified_at is not None
    session.commit.assert_called_once()
    kwargs = send.await_args.kwargs
    assert kwargs["recipient_user_id"] == "person-1"
    assert kwargs["event_gist"] == event.gist
    assert kwargs["notice"].value == FIRST_EVENT_RECOMMENDATION_NOTICE
    assert "subject" not in kwargs
    assert "body_text" not in kwargs
    assert event.text not in repr(send.await_args)


@pytest.mark.asyncio
async def test_failed_event_send_does_not_record_delivery():
    event = _active_event()
    recommendation = EventRecommendation(event_id="event-1", person_id="person-1")
    session = _delivery_session(event, recommendation, prior=0)
    ctx = _ctx(session, event_id="event-1")

    with patch(
        "thenetwork.agent.tools._send_event_fyi",
        new=AsyncMock(
            return_value={"status": "limited", "reason": "recipient_daily_cap"}
        ),
    ):
        result = await send_event_recommendation(ctx, event_id="event-1")

    assert result["status"] == "limited"
    assert recommendation.notified_at is None
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_smtp_exception_does_not_record_event_delivery():
    event = _active_event()
    recommendation = EventRecommendation(event_id="event-1", person_id="person-1")
    session = _delivery_session(event, recommendation, prior=0)
    ctx = _ctx(session, event_id="event-1")

    with (
        patch(
            "thenetwork.agent.tools._send_event_fyi",
            new=AsyncMock(side_effect=OSError("smtp unavailable")),
        ),
        pytest.raises(OSError, match="smtp unavailable"),
    ):
        await send_event_recommendation(ctx, event_id="event-1")

    assert recommendation.notified_at is None
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_later_event_fyi_has_only_concise_event_stop_notice():
    recommendation = EventRecommendation(event_id="event-1", person_id="person-1")
    session = _delivery_session(_active_event(), recommendation, prior=2)
    ctx = _ctx(session, event_id="event-1")

    with patch(
        "thenetwork.agent.tools._send_event_fyi",
        new=AsyncMock(return_value={"status": "sent"}),
    ) as send:
        await send_event_recommendation(ctx, event_id="event-1")

    kwargs = send.await_args.kwargs
    assert kwargs["notice"].value == EVENT_RECOMMENDATION_STOP_NOTICE
    assert kwargs["notice"].value != FIRST_EVENT_RECOMMENDATION_NOTICE
    assert kwargs["event_gist"] == "Event hosted by [name] [email]"
    assert not {"rsvp", "reminder", "attendance", "calendar"}.intersection(
        kwargs["event_gist"].lower().split()
    )


def test_event_send_capability_has_no_model_selected_recipient_or_extra_behavior():
    params = inspect.signature(send_event_recommendation).parameters
    assert "recipient_user_id" not in params
    assert "email" not in params
    assert "subject" not in params
    assert "body_text" not in params
    assert "body_html" not in params
    assert "rsvp" not in params
    assert "reminder" not in params
    assert "attendance" not in params
    assert "calendar" not in params
