"""Periodic semantic discovery for one-way event recommendations.

This pipeline is deliberately independent from introduction discovery. It does
not inspect the projected people graph, introduction consent, or proactive pair
surfaces. Active event embeddings are matched only against sanitized
person-memory embeddings. A recurring event series remains one stable event id,
and the event/person consideration ledger makes that series eligible at most
once for each person.

The scan never schedules event occurrences or follow-on work. Its only output
is a bounded set of synthetic ``process_email`` jobs whose bodies contain
opaque ids and sanitized gists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel import col, select

from thenetwork.db.models import (
    Event,
    EventRecommendation,
    EventSuppression,
    Person,
)
from thenetwork.db.session import get_session
from thenetwork.search.match import match_memories
from thenetwork.settings import get_settings
from thenetwork.worker.tasks import app, process_email


@dataclass(frozen=True)
class _SemanticCandidate:
    event_id: str
    event_version: int
    person_id: str
    event_gist: str
    person_gist: str
    expires_at: datetime
    similarity: float
    memory_id: str


@app.periodic(cron="45 * * * *", periodic_id="scan_for_event_recommendations")
@app.task()
async def scan_for_event_recommendations(timestamp: int) -> None:
    """Select relevant event/person candidates and enqueue sealed agent jobs."""
    settings = get_settings()
    if (
        settings.event_scan_active_event_limit <= 0
        or settings.event_match_top_k <= 0
        or settings.event_scan_max_candidates <= 0
        or settings.event_scan_max_per_person <= 0
    ):
        return

    now = datetime.now(timezone.utc)
    payloads: list[dict] = []

    with get_session() as session:
        # This is a server-side operational projection, not an agent/search
        # result. Raw event text and recurrence are intentionally not loaded.
        events = session.exec(
            select(
                Event.id,
                Event.version,
                Event.gist,
                Event.embedding,
                Event.submitter_id,
                Event.expires_at,
            )
            .where(col(Event.embedding).is_not(None))
            .where(col(Event.cancelled_at).is_(None))
            .where(Event.expires_at > now)
            .order_by(Event.expires_at, Event.id)
            .limit(settings.event_scan_active_event_limit)
        ).all()
        if not events:
            return

        # One person can appear in several matching memories. Keep only their
        # strongest deterministic match for each stable event/series id.
        candidate_by_key: dict[tuple[str, str], _SemanticCandidate] = {}
        for (
            event_id,
            event_version,
            event_gist,
            embedding,
            submitter_id,
            expires_at,
        ) in events:
            for match in match_memories(
                embedding,
                session,
                limit=settings.event_match_top_k,
                min_similarity=settings.event_match_threshold,
            ):
                person_id = match.person_id
                key = (event_id, person_id)
                if (
                    match.similarity < settings.event_match_threshold
                    or person_id == submitter_id
                ):
                    continue

                candidate = _SemanticCandidate(
                    event_id=event_id,
                    event_version=event_version,
                    person_id=person_id,
                    event_gist=event_gist,
                    person_gist=match.gist,
                    expires_at=expires_at,
                    similarity=match.similarity,
                    memory_id=match.memory_id,
                )
                current = candidate_by_key.get(key)
                if current is None or (-candidate.similarity, candidate.memory_id) < (
                    -current.similarity,
                    current.memory_id,
                ):
                    candidate_by_key[key] = candidate

        if not candidate_by_key:
            return

        candidate_person_ids = {person_id for _, person_id in candidate_by_key}
        candidate_event_ids = {event_id for event_id, _ in candidate_by_key}
        people = session.exec(
            select(Person.id, Person.email)
            .where(col(Person.id).in_(candidate_person_ids))
            .order_by(Person.id)
        ).all()
        email_by_id = {person_id: email for person_id, email in people}
        suppressed_people = set(
            session.exec(
                select(EventSuppression.person_id).where(
                    col(EventSuppression.person_id).in_(candidate_person_ids)
                )
            ).all()
        )
        recommendations_by_key = {
            (recommendation.event_id, recommendation.person_id): recommendation
            for recommendation in session.exec(
                select(EventRecommendation)
                .where(col(EventRecommendation.event_id).in_(candidate_event_ids))
                .where(col(EventRecommendation.person_id).in_(candidate_person_ids))
            ).all()
        }

        def is_available(candidate: _SemanticCandidate) -> bool:
            recommendation = recommendations_by_key.get(
                (candidate.event_id, candidate.person_id)
            )
            return recommendation is None or (
                recommendation.notified_at is None
                and recommendation.event_version != candidate.event_version
            )

        candidates = [
            candidate
            for candidate in candidate_by_key.values()
            if candidate.person_id in email_by_id
            and candidate.person_id not in suppressed_people
            and is_available(candidate)
        ]
        candidates.sort(
            key=lambda candidate: (
                -candidate.similarity,
                candidate.event_id,
                candidate.person_id,
                candidate.memory_id,
            )
        )

        per_person: Counter[str] = Counter()
        selected: list[_SemanticCandidate] = []
        for candidate in candidates:
            if per_person[candidate.person_id] >= settings.event_scan_max_per_person:
                continue
            selected.append(candidate)
            per_person[candidate.person_id] += 1
            if len(selected) >= settings.event_scan_max_candidates:
                break

        if not selected:
            return

        for candidate in selected:
            key = (candidate.event_id, candidate.person_id)
            recommendation = recommendations_by_key.get(key)
            if recommendation is None:
                recommendation = EventRecommendation(
                    event_id=candidate.event_id,
                    person_id=candidate.person_id,
                    event_version=candidate.event_version,
                    considered_at=now,
                )
            else:
                recommendation.event_version = candidate.event_version
                recommendation.considered_at = now
            session.add(recommendation)

        # Consideration is durable before any job can run. The unique
        # event/person constraint is the final guard against concurrent scans.
        session.commit()
        payloads = [
            {
                "sender_email": email_by_id[candidate.person_id],
                "subject": "[Proactive] Possible event",
                "body": (
                    "[System event match] A sealed event is semantically relevant "
                    "to a standing signal for this person.\n\n"
                    f"Person {candidate.person_id}: {candidate.person_gist}\n"
                    f"Event {candidate.event_id}: {candidate.event_gist}\n"
                    f"Event expiry: {candidate.expires_at.isoformat()}\n"
                    f"Similarity: {candidate.similarity:.2f}\n\n"
                    f"You are acting for person {candidate.person_id}. Judge "
                    "relevance only from these sanitized gists. If the event is "
                    "specifically useful, send the one-way event FYI with "
                    "`send_event_recommendation(event_id="
                    f"{candidate.event_id})`. Otherwise, take no action."
                ),
                "sender_authenticated": True,
                "is_proactive": True,
                "proactive_event_id": candidate.event_id,
                "proactive_event_version": candidate.event_version,
            }
            for candidate in selected
        ]

    for payload in payloads:
        process_email.defer(**payload, trace_id=str(uuid4()))
