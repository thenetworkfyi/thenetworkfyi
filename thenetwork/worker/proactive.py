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
from uuid import uuid4

from sqlmodel import col, select

from thenetwork.db.models import IntroductionConsent, Memory, Person
from thenetwork.db.session import get_session
from thenetwork.search.graph import build_graph, score_proximity
from thenetwork.search.match import (
    MemoryMatch,
    SealedMemoryEvidence,
    build_candidate_contexts,
    match_memories,
)
from thenetwork.settings import get_settings
from thenetwork.introductions import (
    mark_pairs_surfaced,
    pair_is_suppressed,
    recently_surfaced_pairs,
)
from thenetwork.worker.tasks import app, process_email

PROXIMITY_THRESHOLD = 0.3


def _defer_proactive_jobs(payloads: list[dict]) -> None:
    """Enqueue each synthetic job with its own opaque audit correlation id."""
    for payload in payloads:
        process_email.defer(**payload, trace_id=str(uuid4()))


@app.periodic(cron="0 * * * *", periodic_id="scan_for_opportunities")
@app.task()
async def scan_for_opportunities(timestamp: int) -> None:
    """Hourly scan: find person pairs with high graph proximity, enqueue agent jobs."""
    G = build_graph()
    person_ids = list(G.nodes())

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
        surfaced_pairs = recently_surfaced_pairs(
            session,
            since=now - timedelta(seconds=s.proactive_surface_cooldown_seconds),
        )
        email_cache: dict[str, str | None] = {}
        active_cache: dict[str, bool] = {}
        seen: set[frozenset[str]] = set()
        candidates: list[tuple[float, tuple[str, str], dict]] = []
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
                matches = match_memories(
                    memory.embedding,
                    session,
                    limit=s.proactive_rematch_top_k,
                    min_similarity=s.proactive_match_threshold,
                )
                for match in matches:
                    counterpart_id = match.person_id
                    pair = frozenset((recipient_id, counterpart_id))
                    if (
                        counterpart_id == recipient_id
                        or len(pair) != 2
                        or pair in seen
                        or match.similarity < s.proactive_match_threshold
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
                    if pair_key in surfaced_pairs or email_for(counterpart_id) is None:
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

        candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
        payloads = [payload for _score, _pair_key, payload in candidates]
        selected_pairs = {pair_key for _score, pair_key, _payload in candidates}
        mark_pairs_surfaced(session, selected_pairs, surfaced_at=now)

    _defer_proactive_jobs(payloads)
