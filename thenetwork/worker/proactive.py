"""Proactive outreach triggers: periodic Procrastinate tasks.

Two independent mechanisms surface connection opportunities. Both only
*identify* candidate pairs and `defer` a synthetic `process_email` job - the
agent run decides whether and how to introduce, so the SEAL still governs what
leaves the system.

- `scan_for_opportunities` (graph): high graph-proximity pairs, Jaccard over
  shared neighbours in the projected graph. Needs pre-existing connection
  density to say anything.
- `scan_for_matches` (semantic): periodic rematching of standing notes for
  unengaged people. This re-engages a dormant user when an eligible counterpart
  emerges - no shared connections required.

Both hand the agent only opaque ids + PII-stripped gists in the trigger body,
never raw text, names, or addresses (SEAL).

Both scans order candidates deterministically by descending relevance score and
the canonical pair key. Proposal caps remain enforced at the server boundary in
`introductions.propose_pair`.

"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

from sqlmodel import col, select

from thenetwork.audit import audit_event
from thenetwork.db.models import IntroductionConsent, Memory, Person
from thenetwork.db.session import get_session
from thenetwork.search.graph import build_graph, score_proximity
from thenetwork.search.match import (
    MemoryMatch,
    SealedMemoryEvidence,
    build_candidate_contexts,
    match_memories,
)
from thenetwork.security.token_budget import check_daily_token_budget
from thenetwork.settings import get_settings
from thenetwork.introductions import (
    mark_pairs_surfaced,
    pair_is_suppressed,
    recently_surfaced_pairs,
)
from thenetwork.worker.metrics import record_network_density
from thenetwork.worker.tasks import app, process_email

PROXIMITY_THRESHOLD = 0.3

# Semantic rematching can spend a lower retrieval floor on people whose own
# recent, sealed memory history says waiting is costly. Two writes inside two
# days are a bounded activity signal; an explicit closing-window phrase in a
# recent gist is a separate urgency signal. Each can lower the configured floor
# by 0.05, capped at 0.10 total, so activity improves recall without turning a
# weak keyword hit into an automatic introduction (the agent still judges the
# two-sided thesis). Recently-active senders also rotate candidates within six
# hours instead of spending the default 24-hour cooldown on one candidate.
RECENT_ACTIVITY_WINDOW = timedelta(days=2)
CLOSING_WINDOW_MAX_AGE = timedelta(days=14)
RECENT_ACTIVITY_MEMORY_COUNT = 2
MATCH_THRESHOLD_STEP = 0.05
MAX_MATCH_THRESHOLD_REDUCTION = 0.10
MAX_RECEPTIVENESS_ADJUSTMENT = 0.05
ACTIVE_SURFACE_COOLDOWN_SECONDS = 6 * 60 * 60
_CLOSING_WINDOW_RE = re.compile(
    r"\b(?:"
    r"today|tonight|tomorrow|this (?:week|weekend)|"
    r"in town (?:for )?(?:the next )?(?:\d+|one|two|three|four|five|six|seven) "
    r"(?:hours?|days?|weeks?)|"
    r"(?:until|through|before|deadline(?: is)?|closes?(?: on)?) "
    r"(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{4}-\d{1,2}-\d{1,2})"
    r")\b",
    re.IGNORECASE,
)


def _activity_adjustments(
    memories: list,
    *,
    now: datetime,
    base_threshold: float,
) -> tuple[dict[str, float], set[str]]:
    """Derive bounded per-person floors and activity from sealed memory rows."""
    recent_counts: dict[str, int] = {}
    closing_window_people: set[str] = set()
    for memory in memories:
        age = now - memory.created_at
        person_ids = {person_id for person_id in memory.refs if person_id}
        if timedelta(0) <= age <= RECENT_ACTIVITY_WINDOW:
            for person_id in person_ids:
                recent_counts[person_id] = recent_counts.get(person_id, 0) + 1
        if (
            timedelta(0) <= age <= CLOSING_WINDOW_MAX_AGE
            and memory.gist
            and _CLOSING_WINDOW_RE.search(memory.gist)
        ):
            closing_window_people.update(person_ids)

    recently_active = {
        person_id
        for person_id, count in recent_counts.items()
        if count >= RECENT_ACTIVITY_MEMORY_COUNT
    } | closing_window_people
    effective_thresholds: dict[str, float] = {}
    for person_id in set(recent_counts) | closing_window_people:
        signal_count = int(
            recent_counts.get(person_id, 0) >= RECENT_ACTIVITY_MEMORY_COUNT
        ) + int(person_id in closing_window_people)
        reduction = min(
            signal_count * MATCH_THRESHOLD_STEP,
            MAX_MATCH_THRESHOLD_REDUCTION,
        )
        effective_thresholds[person_id] = max(0.0, base_threshold - reduction)
    return effective_thresholds, recently_active


def _receptiveness_adjustments(consent_rows: list) -> dict[str, float]:
    """Derive a bounded counterpart prior from server-owned consent history.

    An explicit consent is positive evidence for that participant. A declined
    pair is negative evidence only for a participant who had not consented;
    unresolved proposals carry no behavioral signal. People with no such
    history remain neutral.
    """
    outcomes: dict[str, list[int]] = {}
    for row in consent_rows:
        participants = (
            (row.person_a_id, row.person_a_consented),
            (row.person_b_id, row.person_b_consented),
        )
        for person_id, consented in participants:
            if consented:
                outcomes.setdefault(person_id, []).append(1)
            elif row.status == "declined":
                outcomes.setdefault(person_id, []).append(-1)

    return {
        person_id: max(
            -MAX_RECEPTIVENESS_ADJUSTMENT,
            min(
                MAX_RECEPTIVENESS_ADJUSTMENT,
                (sum(person_outcomes) / len(person_outcomes))
                * MAX_RECEPTIVENESS_ADJUSTMENT,
            ),
        )
        for person_id, person_outcomes in outcomes.items()
    }


def _defer_proactive_jobs(payloads: list[dict]) -> None:
    """Enqueue each synthetic job with its own opaque audit correlation id.

    Checked against the daily token budget before deferring: these two scans
    call ``process_email.defer`` directly, bypassing the producer's own
    pre-check entirely (see worker/producer.py). Unlike inbound mail there is
    no sender to notify or re-poll later - a skipped candidate pair simply
    regenerates on a later scan, so this is a silent drop.
    """
    if not payloads:
        return
    if not check_daily_token_budget(get_settings().daily_agent_token_cap):
        audit_event(
            "worker.message_rejected",
            reason="daily_token_budget_exhausted",
            message_count=len(payloads),
        )
        return
    for payload in payloads:
        process_email.defer(**payload, trace_id=str(uuid4()))


@app.periodic(cron="0 * * * *", periodic_id="scan_for_opportunities")
@app.task()
async def scan_for_opportunities(timestamp: int) -> None:
    """Hourly scan: find person pairs with high graph proximity, enqueue agent jobs."""
    G = build_graph()
    person_ids = list(G.nodes())
    record_network_density(
        avg_degree=(2 * G.number_of_edges() / len(person_ids)) if person_ids else 0.0
    )

    if len(person_ids) < 2:
        return

    s = get_settings()
    now = datetime.now(timezone.utc)
    with get_session() as session:
        people = session.exec(
            select(Person).where(col(Person.id).in_(person_ids))
        ).all()
        email_by_id = {p.id: p.email for p in people}
        surfaced_pairs = recently_surfaced_pairs(
            session,
            since=now - timedelta(seconds=s.proactive_surface_cooldown_seconds),
        )

        candidates: list[tuple[float, tuple[str, str], dict]] = []
        for i, pid_a in enumerate(person_ids):
            if pid_a not in email_by_id:
                continue
            scores = score_proximity(pid_a, person_ids[i + 1 :], graph=G)
            for pid_b in person_ids[i + 1 :]:
                score = scores[pid_b]
                if score < PROXIMITY_THRESHOLD:
                    continue
                if pair_is_suppressed(session, pid_a, pid_b):
                    continue
                pair_key: tuple[str, str] = tuple(sorted((pid_a, pid_b)))  # type: ignore[assignment]
                if pair_key in surfaced_pairs:
                    continue
                candidates.append(
                    (
                        score,
                        pair_key,
                        {
                            "sender_email": email_by_id[pid_a],
                            "subject": "[Proactive] Potential connection",
                            "body": (
                                f"[System trigger] You are acting for person "
                                f"{pid_a}, who has a high-proximity match "
                                f"(score={score:.2f}) with person {pid_b}. "
                                f"Consider reaching out. If you propose an "
                                f"introduction, pass other_person_id={pid_b} "
                                f"(the counterpart) - never the id of the "
                                f"person you are acting for; their side of "
                                f"the pair is derived server-side."
                            ),
                            "sender_authenticated": True,
                            "is_proactive": True,
                            "proactive_candidate_id": pid_b,
                        },
                    )
                )

        candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
        payloads = [payload for _score, _pair_key, payload in candidates]
        selected_pairs = {pair_key for _score, pair_key, _payload in candidates}
        mark_pairs_surfaced(session, selected_pairs, surfaced_at=now)

    _defer_proactive_jobs(payloads)


@app.periodic(cron="30 * * * *", periodic_id="scan_for_matches")
@app.task()
async def scan_for_matches(timestamp: int) -> None:
    """Periodically surface each unengaged person's best semantic counterpart.

    Every run re-evaluates sanitized person-referencing memories rather than
    relying on a narrow arrival window. A person with no active consent pair
    considers every eligible counterpart each run; pairs can be reconsidered
    after the proactive-surface cooldown when an earlier attempt did not lead
    to a proposal. Trigger bodies contain only opaque ids and sanitized gists.
    """
    s = get_settings()
    now = datetime.now(timezone.utc)

    with get_session() as session:
        memories = session.exec(
            select(
                Memory.id,
                Memory.refs,
                Memory.gist,
                Memory.embedding,
                Memory.created_at,
            )
            .where(col(Memory.gist).is_not(None))
            .where(col(Memory.embedding).is_not(None))
            .order_by(col(Memory.created_at).desc())
            .limit(500)
        ).all()
        if not memories:
            return

        graph = build_graph()
        effective_thresholds, recently_active = _activity_adjustments(
            memories,
            now=now,
            base_threshold=s.proactive_match_threshold,
        )
        consent_history = session.exec(
            select(
                IntroductionConsent.person_a_id,
                IntroductionConsent.person_b_id,
                IntroductionConsent.person_a_consented,
                IntroductionConsent.person_b_consented,
                IntroductionConsent.status,
            )
        ).all()
        receptiveness_adjustments = _receptiveness_adjustments(consent_history)
        surfaced_pairs = recently_surfaced_pairs(
            session,
            since=now - timedelta(seconds=s.proactive_surface_cooldown_seconds),
        )
        active_cooldown_seconds = min(
            s.proactive_surface_cooldown_seconds,
            ACTIVE_SURFACE_COOLDOWN_SECONDS,
        )
        recently_active_surfaced_pairs = recently_surfaced_pairs(
            session,
            since=now - timedelta(seconds=active_cooldown_seconds),
        )
        email_cache: dict[str, str | None] = {}
        active_cache: dict[str, bool] = {}
        seen: set[frozenset[str]] = set()
        candidates: list[tuple[float, float, tuple[str, str], dict]] = []
        recent_evidence: dict[str, list[SealedMemoryEvidence]] = {}
        for memory in memories:
            if not memory.gist:
                continue
            for person_id in (person_id for person_id in memory.refs if person_id):
                recent_evidence.setdefault(person_id, []).append(
                    SealedMemoryEvidence(
                        memory_id=memory.id,
                        gist=memory.gist,
                    )
                )

        def email_for(person_id: str) -> str | None:
            if person_id not in email_cache:
                person = session.get(Person, person_id)
                email_cache[person_id] = person.email if person is not None else None
            return email_cache[person_id]

        def has_active_consent(person_id: str) -> bool:
            if person_id not in active_cache:
                active_cache[person_id] = bool(
                    session.exec(
                        select(IntroductionConsent.id).where(
                            (IntroductionConsent.person_a_id == person_id)
                            | (IntroductionConsent.person_b_id == person_id),
                            col(IntroductionConsent.status).in_(
                                ("proposed", "one_consented", "introduced")
                            ),
                        )
                    ).first()
                )
            return active_cache[person_id]

        for memory in memories:
            for recipient_id in (person_id for person_id in memory.refs if person_id):
                if has_active_consent(recipient_id) or email_for(recipient_id) is None:
                    continue
                effective_threshold = effective_thresholds.get(
                    recipient_id, s.proactive_match_threshold
                )
                matches = match_memories(
                    memory.embedding,
                    session,
                    limit=s.proactive_rematch_top_k,
                    min_similarity=max(
                        0.0,
                        s.proactive_match_threshold
                        - MAX_MATCH_THRESHOLD_REDUCTION
                        - MAX_RECEPTIVENESS_ADJUSTMENT,
                    ),
                )
                for match in matches:
                    counterpart_id = match.person_id
                    receptiveness_adjustment = receptiveness_adjustments.get(
                        counterpart_id, 0.0
                    )
                    candidate_threshold = min(
                        1.0,
                        max(0.0, effective_threshold - receptiveness_adjustment),
                    )
                    pair = frozenset((recipient_id, counterpart_id))
                    if (
                        counterpart_id == recipient_id
                        or len(pair) != 2
                        or pair in seen
                        or match.similarity < candidate_threshold
                        or graph.has_edge(recipient_id, counterpart_id)
                        or pair_is_suppressed(
                            session,
                            recipient_id,
                            counterpart_id,
                            decline_cooldown_days=s.consent_decline_cooldown_days,
                        )
                    ):
                        continue
                    pair_key: tuple[str, str] = tuple(sorted(pair))  # type: ignore[assignment]
                    recipient_surfaced_pairs = (
                        recently_active_surfaced_pairs
                        if recipient_id in recently_active
                        else surfaced_pairs
                    )
                    if (
                        pair_key in recipient_surfaced_pairs
                        or email_for(counterpart_id) is None
                    ):
                        continue
                    seen.add(pair)
                    pair_contexts = {
                        context.person_id: context
                        for context in build_candidate_contexts(
                            [
                                MemoryMatch(
                                    memory_id=memory.id,
                                    person_id=recipient_id,
                                    gist=memory.gist,
                                    similarity=match.similarity,
                                ),
                                match,
                            ],
                            recent_evidence,
                            max_candidates=2,
                        )
                    }
                    recipient_evidence = pair_contexts[recipient_id].evidence
                    counterpart_evidence = pair_contexts[counterpart_id].evidence
                    recipient_lines = "\n".join(
                        f"- {item.gist}" for item in recipient_evidence
                    )
                    counterpart_lines = "\n".join(
                        f"- {item.gist}" for item in counterpart_evidence
                    )
                    body = (
                        "[System match] Semantic retrieval surfaced a candidate pair "
                        f"(similarity={match.similarity:.2f}, a retrieval signal, not "
                        "a fit score). The evidence below is bounded to sealed gists "
                        "grouped by opaque person id.\n\n"
                        f"Person {recipient_id} evidence:\n{recipient_lines}\n\n"
                        f"Person {counterpart_id} evidence:\n"
                        f"{counterpart_lines}\n\n"
                        f"You are acting for person {recipient_id}, the recipient "
                        "of this trigger. Their side of the pair is derived "
                        "server-side, so never pass their id to "
                        "`propose_introduction`. If these two share specific, "
                        "two-sided, materially supported common ground, propose an "
                        "introduction with "
                        f"`propose_introduction` and other_person_id={counterpart_id} "
                        "(the counterpart), using only what the gists support. "
                        "If a consequential side or constraint is missing, "
                        "contradictory, or supported only by keyword overlap, call "
                        "`no_action`."
                    )
                    candidates.append(
                        (
                            match.similarity + receptiveness_adjustment,
                            match.similarity,
                            pair_key,
                            {
                                "sender_email": email_for(recipient_id),
                                "subject": "[Proactive] Possible connection",
                                "body": body,
                                "sender_authenticated": True,
                                "is_proactive": True,
                                "proactive_candidate_id": counterpart_id,
                            },
                        )
                    )

        candidates.sort(
            key=lambda candidate: (-candidate[0], -candidate[1], candidate[2])
        )
        payloads = [payload for _rank, _score, _pair_key, payload in candidates]
        selected_pairs = {pair_key for _rank, _score, pair_key, _payload in candidates}
        mark_pairs_surfaced(session, selected_pairs, surfaced_at=now)

    _defer_proactive_jobs(payloads)
