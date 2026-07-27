"""Assembled event-recommendation flows against a real pgvector database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import select

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.tools import (
    FIRST_EVENT_RECOMMENDATION_NOTICE,
    create_event,
    send_event_recommendation,
    stop_event_recommendations,
    update_event,
)
from thenetwork.db.models import Event, EventRecommendation, EventSuppression
from thenetwork.db.session import get_session
from thenetwork.introductions import ConsentReplyResult
from thenetwork.search.match import MemoryMatch
from thenetwork.worker.event_scan import scan_for_event_recommendations
from thenetwork.worker.proactive import scan_for_matches
from thenetwork.worker.tasks import process_email


def _ctx(
    *,
    email: str,
    person_id: str,
    event_id: str | None = None,
    event_version: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        deps=AgentDeps(
            sender_email=email,
            sender_user_id=person_id,
            sender_authenticated=True,
            is_proactive=event_id is not None,
            proactive_event_id=event_id,
            proactive_event_version=event_version,
        )
    )


def _event_scan_settings(**overrides) -> SimpleNamespace:
    values = {
        "event_match_threshold": 0.95,
        "event_match_top_k": 20,
        "event_scan_active_event_limit": 100,
        "event_scan_max_candidates": 50,
        "event_scan_max_per_person": 1,
        "daily_agent_token_cap": 0,
        **overrides,
    }
    return SimpleNamespace(**values)


def _people_scan_settings() -> SimpleNamespace:
    return SimpleNamespace(
        proactive_surface_cooldown_seconds=86_400,
        proactive_rematch_top_k=20,
        proactive_match_threshold=0.6,
        consent_decline_cooldown_days=90,
    )


@pytest.mark.integration
async def test_submit_scan_send_suppress_and_people_match_remains_eligible(seeded_db):
    """Exercise the assembled event path and its separation from people matching."""
    owner_id = seeded_db["alice_id"]
    recipient_id = seeded_db["bob_id"]
    event_embedding = seeded_db["query_ml"].copy()
    event_embedding[0] = 0.0
    event_embedding[1] = 1.0
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    raw_submission = "Owner-private compiler circle details"
    sealed_gist = "small compiler engineering circle"

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text",
            new=MagicMock(return_value=sealed_gist),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=event_embedding),
        ),
    ):
        created = await create_event(
            _ctx(email="alice@test.com", person_id=owner_id),
            text=raw_submission,
            expires_at=expiry.isoformat(),
            recurrence="every Friday",
        )

    assert created["status"] == "created"
    event_id = created["event_id"]
    assert created["gist"] == sealed_gist
    assert raw_submission not in repr(created)

    with get_session() as session:
        stored = session.get(Event, event_id)
        assert stored is not None
        assert stored.text == raw_submission
        assert stored.gist == sealed_gist
        assert stored.recurrence == "every Friday"
        expired_event = Event(
            submitter_id=owner_id,
            text="expired raw event",
            gist="expired compiler event",
            embedding=event_embedding,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        cancelled_event = Event(
            submitter_id=owner_id,
            text="cancelled raw event",
            gist="cancelled compiler event",
            embedding=event_embedding,
            expires_at=expiry,
            cancelled_at=datetime.now(timezone.utc),
        )
        session.add_all([expired_event, cancelled_event])
        session.commit()
        lifecycle_event_ids = [event_id, expired_event.id, cancelled_event.id]

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_event_scan_settings(),
        ),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    deferred.defer.assert_called_once()
    job = deferred.defer.call_args.kwargs
    assert job["sender_email"] == "bob@test.com"
    assert job["sender_authenticated"] is True
    assert job["proactive_event_id"] == event_id
    assert raw_submission not in job["body"]
    assert sealed_gist in job["body"]

    model_calls = 0

    async def send_bound_event(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="send_event_recommendation",
                        args={"event_id": event_id},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Event FYI sent.")])

    deterministic_agent = build_agent(model=FunctionModel(send_bound_event))
    delivered: list[dict] = []

    with (
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
        patch("thenetwork.agent.core.build_agent", return_value=deterministic_agent),
        patch("thenetwork.agent.core.audit_model_trace"),
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap"),
        patch(
            "thenetwork.agent.tools.send_reply",
            side_effect=lambda **kwargs: delivered.append(kwargs),
        ),
    ):
        await process_email.func(**job)

    assert model_calls == 2
    assert len(delivered) == 1
    assert delivered[0]["to_address"] == "bob@test.com"
    assert sealed_gist in delivered[0]["body_text"]
    assert FIRST_EVENT_RECOMMENDATION_NOTICE in delivered[0]["body_text"]
    assert raw_submission not in repr(delivered)
    lowered_delivery = delivered[0]["body_text"].lower()
    assert all(
        unsupported not in lowered_delivery
        for unsupported in (
            "reminder",
            "rsvp",
            "attendance",
            "follow-up",
            "calendar",
            "people recommendations",
        )
    )

    with get_session() as session:
        recommendation = session.exec(
            select(EventRecommendation).where(
                EventRecommendation.event_id == event_id,
                EventRecommendation.person_id == recipient_id,
            )
        ).one()
        assert recommendation.notified_at is not None
        all_recommendations = session.exec(
            select(EventRecommendation).where(
                EventRecommendation.person_id == recipient_id,
                EventRecommendation.event_id.in_(lifecycle_event_ids),
            )
        ).all()
        assert [(row.event_id, row.person_id) for row in all_recommendations] == [
            (event_id, recipient_id)
        ]

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_event_scan_settings(),
        ),
        patch("thenetwork.worker.event_scan.process_email") as deferred_again,
    ):
        await scan_for_event_recommendations.func(1)
    deferred_again.defer.assert_not_called()

    stopped = await stop_event_recommendations(
        _ctx(email="bob@test.com", person_id=recipient_id)
    )
    assert stopped == {"status": "suppressed"}

    with get_session() as session:
        second_event = Event(
            submitter_id=owner_id,
            text="second raw event",
            gist="another compiler engineering circle",
            embedding=event_embedding,
            expires_at=expiry,
        )
        session.add(second_event)
        session.commit()
        second_event_id = second_event.id
        assert session.get(EventSuppression, recipient_id) is not None

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_event_scan_settings(),
        ),
        patch("thenetwork.worker.event_scan.process_email") as suppressed_event,
    ):
        await scan_for_event_recommendations.func(2)
    suppressed_event.defer.assert_not_called()
    with get_session() as session:
        assert (
            session.exec(
                select(EventRecommendation).where(
                    EventRecommendation.event_id == second_event_id,
                    EventRecommendation.person_id == recipient_id,
                )
            ).first()
            is None
        )

    people_match = MemoryMatch(
        memory_id=seeded_db["mem_alice_id"],
        person_id=owner_id,
        gist="ml engineer",
        similarity=0.99,
    )
    with (
        patch(
            "thenetwork.worker.proactive.get_settings",
            return_value=_people_scan_settings(),
        ),
        patch(
            "thenetwork.worker.proactive.match_memories",
            return_value=[people_match],
        ),
        patch("thenetwork.worker.proactive.process_email") as people_deferred,
    ):
        await scan_for_matches.func(3)

    assert any(
        call.kwargs["sender_email"] == "bob@test.com"
        and call.kwargs["proactive_candidate_id"] == owner_id
        for call in people_deferred.defer.call_args_list
    )


@pytest.mark.integration
async def test_updated_event_requires_a_fresh_version_bound_evaluation(seeded_db):
    owner_id = seeded_db["alice_id"]
    recipient_id = seeded_db["bob_id"]
    recipient_embedding = [0.0] * 1536
    recipient_embedding[1] = 1.0
    expiry = datetime.now(timezone.utc) + timedelta(days=30)

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text",
            new=MagicMock(return_value="relevant compiler engineering circle"),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=recipient_embedding),
        ),
    ):
        created = await create_event(
            _ctx(email="alice@test.com", person_id=owner_id),
            text="original relevant event",
            expires_at=expiry.isoformat(),
        )

    event_id = created["event_id"]
    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_event_scan_settings(),
        ),
        patch("thenetwork.worker.event_scan.process_email") as first_deferred,
    ):
        await scan_for_event_recommendations.func(0)

    stale_job = first_deferred.defer.call_args.kwargs
    assert stale_job["proactive_event_version"] == 1
    assert "relevant compiler engineering circle" in stale_job["body"]

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text",
            new=MagicMock(return_value="unrelated online cooking webinar"),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=recipient_embedding),
        ),
    ):
        updated = await update_event(
            _ctx(email="alice@test.com", person_id=owner_id),
            event_id=event_id,
            text="replacement unrelated event",
            expires_at=expiry.isoformat(),
        )
    assert updated["status"] == "updated"

    stale_ctx = _ctx(
        email="bob@test.com",
        person_id=recipient_id,
        event_id=event_id,
        event_version=stale_job["proactive_event_version"],
    )
    with patch(
        "thenetwork.agent.tools._send_email", new_callable=AsyncMock
    ) as stale_send:
        stale_result = await send_event_recommendation(stale_ctx, event_id)

    assert stale_result == {
        "status": "suppressed",
        "reason": "event_version_changed",
    }
    stale_send.assert_not_awaited()

    with get_session() as session:
        event = session.get(Event, event_id)
        recommendation = session.exec(
            select(EventRecommendation).where(
                EventRecommendation.event_id == event_id,
                EventRecommendation.person_id == recipient_id,
            )
        ).one()
        assert event is not None and event.version == 2
        assert recommendation.event_version == 1
        assert recommendation.notified_at is None

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_event_scan_settings(),
        ),
        patch("thenetwork.worker.event_scan.process_email") as refreshed_deferred,
    ):
        await scan_for_event_recommendations.func(1)

    refreshed_job = refreshed_deferred.defer.call_args.kwargs
    assert refreshed_job["proactive_event_version"] == 2
    assert "unrelated online cooking webinar" in refreshed_job["body"]
    with get_session() as session:
        refreshed = session.exec(
            select(EventRecommendation).where(
                EventRecommendation.event_id == event_id,
                EventRecommendation.person_id == recipient_id,
            )
        ).one()
        assert refreshed.event_version == 2
        assert refreshed.notified_at is None


@pytest.mark.integration
async def test_event_scan_over_budget_leaves_no_orphaned_recommendation_row(
    seeded_db, pg_engine
):
    """A scan run while the daily token budget is exhausted must not commit
    any event_recommendations row for the otherwise-eligible candidate: a
    committed pending row would permanently suppress re-selection of this
    event/person pair once the budget recovers, silently losing the
    recommendation rather than merely delaying it."""
    import thenetwork.security.token_budget as token_budget
    from sqlalchemy import text

    owner_id = seeded_db["alice_id"]
    recipient_id = seeded_db["bob_id"]
    event_embedding = seeded_db["query_ml"].copy()
    event_embedding[0] = 0.0
    event_embedding[1] = 1.0
    expiry = datetime.now(timezone.utc) + timedelta(days=30)

    with (
        patch(
            "thenetwork.agent.tools.sanitize_text",
            new=MagicMock(return_value="small compiler engineering circle"),
        ),
        patch(
            "thenetwork.agent.tools.embed_text",
            new=AsyncMock(return_value=event_embedding),
        ),
    ):
        created = await create_event(
            _ctx(email="alice@test.com", person_id=owner_id),
            text="owner-private compiler circle details",
            expires_at=expiry.isoformat(),
        )
    event_id = created["event_id"]

    token_budget._limiter = None
    token_budget._storage = None
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits"))
    assert token_budget.consume_daily_token_budget(1, 1) is True

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_event_scan_settings(daily_agent_token_cap=1),
        ),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    deferred.defer.assert_not_called()
    with get_session() as session:
        assert (
            session.exec(
                select(EventRecommendation).where(
                    EventRecommendation.event_id == event_id,
                    EventRecommendation.person_id == recipient_id,
                )
            ).first()
            is None
        )

    token_budget._limiter = None
    token_budget._storage = None
