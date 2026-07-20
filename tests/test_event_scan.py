"""Worker tests for independent event recommendation discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from thenetwork.db.models import EventRecommendation
from thenetwork.search.match import MemoryMatch


def _settings(**overrides):
    values = {
        "event_match_threshold": 0.6,
        "event_match_top_k": 20,
        "event_scan_active_event_limit": 100,
        "event_scan_max_candidates": 50,
        "event_scan_max_per_person": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _event_row(
    event_id: str,
    *,
    version: int = 1,
    gist: str = "sealed compiler meetup",
    embedding: list[float] | None = None,
    submitter_id: str = "owner",
    expires_at: datetime | None = None,
):
    return (
        event_id,
        version,
        gist,
        embedding or [1.0, 0.0],
        submitter_id,
        expires_at or datetime.now(timezone.utc) + timedelta(days=3),
    )


def _scan_session(
    *, events, people, suppressed=(), considered=(), timeline=None, claim_results=()
):
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    claim_results = iter(claim_results)

    def execute(statement):
        query = str(statement).lower()
        result = MagicMock()
        if query.startswith(
            ("insert into event_recommendations", "update event_recommendations")
        ):
            result.first.return_value = next(claim_results, "claimed")
            return result
        elif "from events" in query:
            rows = events
        elif "from people" in query:
            rows = people
        elif "from event_suppressions" in query:
            rows = list(suppressed)
        elif "from event_recommendations" in query:
            rows = list(considered)
        else:
            raise AssertionError(f"unexpected scan query: {query}")
        result.all.return_value = rows
        return result

    session.exec.side_effect = execute
    if timeline is not None:
        session.commit.side_effect = lambda: timeline.append("commit")
    return session


@pytest.mark.asyncio
async def test_event_scan_selects_only_semantically_relevant_eligible_audience():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    expiry = datetime.now(timezone.utc) + timedelta(days=3)
    session = _scan_session(
        events=[_event_row("event-1", expires_at=expiry)],
        people=[
            ("owner", "owner-raw@example.com"),
            ("relevant", "relevant-raw@example.com"),
            ("irrelevant", "irrelevant-raw@example.com"),
        ],
    )
    matches = [
        MemoryMatch("m-owner", "owner", "owner private signal", 0.99),
        MemoryMatch("m-missing", "missing", "missing person signal", 0.95),
        MemoryMatch("m-good", "relevant", "sealed compiler interest", 0.82),
        # Keep the scan's own threshold check even if a backend regresses.
        MemoryMatch("m-thin", "irrelevant", "thin keyword overlap", 0.4),
    ]
    timeline: list[str] = []
    session.commit.side_effect = lambda: timeline.append("commit")

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_settings(),
        ),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch(
            "thenetwork.worker.event_scan.match_memories", return_value=matches
        ) as match,
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        deferred.defer.side_effect = lambda **_kwargs: timeline.append("defer")
        await scan_for_event_recommendations.func(0)

    match.assert_called_once_with([1.0, 0.0], session, limit=20, min_similarity=0.6)
    deferred.defer.assert_called_once()
    job = deferred.defer.call_args.kwargs
    assert job["sender_email"] == "relevant-raw@example.com"
    assert job["sender_authenticated"] is True
    assert job["is_proactive"] is True
    assert job["proactive_event_id"] == "event-1"
    assert job["proactive_event_version"] == 1
    assert str(UUID(job["trace_id"], version=4)) == job["trace_id"]
    assert "Person relevant: sealed compiler interest" in job["body"]
    assert "Event event-1: sealed compiler meetup" in job["body"]
    assert expiry.isoformat() in job["body"]
    assert "raw@example.com" not in job["body"]
    assert "owner private" not in job["body"]
    assert "thin keyword" not in job["body"]
    assert "proactive_candidate_id" not in job
    assert timeline == ["commit", "defer"]

    claim = session.exec.call_args_list[-1].args[0]
    assert "on conflict (event_id, person_id) do nothing" in str(claim).lower()

    event_query = str(session.exec.call_args_list[0].args[0]).lower()
    assert "cancelled_at is null" in event_query
    assert "expires_at >" in event_query
    assert "events.text" not in event_query
    assert "events.recurrence" not in event_query


@pytest.mark.asyncio
async def test_event_scan_query_excludes_expired_cancelled_and_unembedded_events():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    session = _scan_session(events=[], people=[])
    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_settings(),
        ),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories") as match,
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    event_query = str(session.exec.call_args.args[0]).lower()
    assert "events.embedding is not null" in event_query
    assert "events.cancelled_at is null" in event_query
    assert "events.expires_at >" in event_query
    match.assert_not_called()
    deferred.defer.assert_not_called()


@pytest.mark.asyncio
async def test_event_scan_skips_suppressed_and_previously_considered_people():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    session = _scan_session(
        events=[_event_row("event-1")],
        people=[("suppressed", "s@example.com"), ("considered", "c@example.com")],
        suppressed=["suppressed"],
        considered=[
            EventRecommendation(
                event_id="event-1", person_id="considered", event_version=1
            )
        ],
    )
    matches = [
        MemoryMatch("m-s", "suppressed", "sealed relevant signal", 0.9),
        MemoryMatch("m-c", "considered", "sealed relevant signal", 0.8),
    ]

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_settings(),
        ),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories", return_value=matches),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    session.add.assert_not_called()
    session.commit.assert_not_called()
    deferred.defer.assert_not_called()


@pytest.mark.asyncio
async def test_event_scan_orders_stably_and_applies_per_person_and_global_caps():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    session = _scan_session(
        events=[_event_row("event-2"), _event_row("event-1")],
        people=[
            (person, f"{person}@example.com") for person in ("p1", "p2", "p3", "p4")
        ],
    )
    matches = [
        [
            MemoryMatch("m2-p2", "p2", "p2 signal", 0.9),
            MemoryMatch("m2-p1", "p1", "p1 second event", 0.8),
            MemoryMatch("m2-p4", "p4", "p4 second event", 0.7),
        ],
        [
            MemoryMatch("m1-p3", "p3", "p3 signal", 0.9),
            MemoryMatch("m1-p1", "p1", "p1 best event", 0.9),
            MemoryMatch("m1-p4", "p4", "p4 strongest signal", 0.95),
        ],
    ]

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_settings(
                event_scan_max_candidates=4,
                event_scan_max_per_person=1,
            ),
        ),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories", side_effect=matches),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    assert [
        (call.kwargs["proactive_event_id"], call.kwargs["sender_email"])
        for call in deferred.defer.call_args_list
    ] == [
        ("event-1", "p4@example.com"),
        ("event-1", "p1@example.com"),
        ("event-1", "p3@example.com"),
        ("event-2", "p2@example.com"),
    ]
    claims = [
        call.args[0]
        for call in session.exec.call_args_list
        if str(call.args[0]).lower().startswith("insert into event_recommendations")
    ]
    assert len(claims) == 4


@pytest.mark.asyncio
async def test_event_scan_refreshes_a_pending_consideration_for_an_updated_event():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    stale = EventRecommendation(
        event_id="event-1", person_id="person-1", event_version=1
    )
    session = _scan_session(
        events=[_event_row("event-1", version=2, gist="sealed updated event")],
        people=[("person-1", "person@example.com")],
        considered=[stale],
    )
    matches = [MemoryMatch("memory-1", "person-1", "sealed interest", 0.9)]

    with (
        patch("thenetwork.worker.event_scan.get_settings", return_value=_settings()),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories", return_value=matches),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    assert deferred.defer.call_args.kwargs["proactive_event_version"] == 2
    assert "sealed updated event" in deferred.defer.call_args.kwargs["body"]
    claim = session.exec.call_args_list[-1].args[0]
    rendered = str(claim).lower()
    assert rendered.startswith("update event_recommendations")
    assert "notified_at is null" in rendered
    assert "event_version !=" in rendered


@pytest.mark.asyncio
async def test_recurring_series_is_considered_once_per_person_at_best_match():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    session = _scan_session(
        events=[_event_row("stable-series-id")],
        people=[("person-1", "person@example.com")],
    )
    matches = [
        MemoryMatch("memory-z", "person-1", "weaker sealed signal", 0.7),
        MemoryMatch("memory-a", "person-1", "stronger sealed signal", 0.85),
    ]

    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_settings(event_scan_max_per_person=5),
        ),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories", return_value=matches),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    deferred.defer.assert_called_once()
    body = deferred.defer.call_args.kwargs["body"]
    assert "stronger sealed signal" in body
    assert "weaker sealed signal" not in body
    claim = session.exec.call_args_list[-1].args[0]
    assert "on conflict (event_id, person_id) do nothing" in str(claim).lower()
    event_query = str(session.exec.call_args_list[0].args[0]).lower()
    assert "recurrence" not in event_query
    assert not {
        "occurrence",
        "reminder",
        "follow_up",
        "rsvp",
        "attendance",
        "calendar",
    }.intersection(deferred.defer.call_args.kwargs)


@pytest.mark.asyncio
async def test_event_scan_enqueues_only_the_atomic_ledger_claim_winner():
    from thenetwork.worker.event_scan import scan_for_event_recommendations

    session = _scan_session(
        events=[_event_row("event-1")],
        people=[("person-1", "person@example.com")],
        # The second scan is a concurrent loser at INSERT ... ON CONFLICT.
        claim_results=("recommendation-1", None),
    )
    matches = [MemoryMatch("memory-1", "person-1", "sealed interest", 0.9)]

    with (
        patch("thenetwork.worker.event_scan.get_settings", return_value=_settings()),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories", return_value=matches),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)
        await scan_for_event_recommendations.func(1)

    deferred.defer.assert_called_once()
    assert deferred.defer.call_args.kwargs["proactive_event_id"] == "event-1"
    assert deferred.defer.call_args.kwargs["proactive_event_version"] == 1
    assert session.commit.call_count == 2


@pytest.mark.asyncio
async def test_event_scan_job_reaches_agent_through_real_worker_handoff():
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.worker.event_scan import scan_for_event_recommendations
    from thenetwork.worker.tasks import process_email

    session = _scan_session(
        events=[_event_row("event-1")],
        people=[("person-1", "person@example.com")],
    )
    matches = [MemoryMatch("memory-1", "person-1", "sealed interest", 0.8)]
    with (
        patch(
            "thenetwork.worker.event_scan.get_settings",
            return_value=_settings(),
        ),
        patch("thenetwork.worker.event_scan.get_session", return_value=session),
        patch("thenetwork.worker.event_scan.match_memories", return_value=matches),
        patch("thenetwork.worker.event_scan.process_email") as deferred,
    ):
        await scan_for_event_recommendations.func(0)

    job = deferred.defer.call_args.kwargs
    worker_session = MagicMock()
    worker_session.__enter__ = MagicMock(return_value=worker_session)
    worker_session.__exit__ = MagicMock(return_value=False)
    worker_session.get.return_value = None
    worker_session.exec.return_value.first.return_value = "person-1"

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=worker_session),
        patch(
            "thenetwork.worker.tasks.check_rate_limit", return_value=True
        ) as rate_limit,
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
        await process_email.func(**job)

    rate_limit.assert_called_once_with(
        "person@example.com", sender_authenticated=True, skip_sender_limit=True
    )
    run_agent.assert_awaited_once()
    kwargs = run_agent.call_args.kwargs
    assert kwargs["sender_user_id"] == "person-1"
    assert kwargs["sender_authenticated"] is True
    assert kwargs["is_proactive"] is True
    assert kwargs["proactive_event_id"] == "event-1"
    assert kwargs["proactive_event_version"] == 1
    assert kwargs["email_body"] == job["body"]


def test_worker_registers_independent_event_scan_module():
    from thenetwork.worker.tasks import app

    assert "thenetwork.worker.event_scan" in app.import_paths
    assert "thenetwork.worker.abuse_judge" in app.import_paths
