"""Proactive outreach triggers: periodic Procrastinate tasks.

Two independent mechanisms surface connection opportunities. Both only
*identify* candidate pairs and `defer` a synthetic `process_email` job - the
agent run decides whether and how to introduce, so the SEAL still governs what
leaves the system.

- `scan_for_opportunities` (graph): high graph-proximity pairs, Jaccard over
  shared neighbours in the projected graph. Needs pre-existing connection
  density to say anything.
- `scan_for_matches` (semantic): a newly-arrived memory that now closely
  matches an *older* standing note about a different person. This is what lets
  the system re-engage a dormant user weeks later, when someone who finally
  fits what they told us first shows up - no shared connections required.

Both hand the agent only opaque ids + PII-stripped gists in the trigger body,
never raw text, names, or addresses (SEAL).

Both scans are *paced*: candidates are collected, ordered deterministically
(score descending, then the canonical pair key), and each person is scheduled
for at most one new candidate per scan. A dense cluster - e.g. several
manufacturing-adjacent members arriving together - therefore surfaces as a few
best-first pairs per hour instead of a combinatorial burst of proposals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import networkx as nx
from sqlmodel import col, select

from thenetwork.db.models import Memory, Person
from thenetwork.db.session import get_session
from thenetwork.search.graph import build_graph
from thenetwork.search.match import match_memories
from thenetwork.settings import get_settings
from thenetwork.introductions import pair_is_suppressed
from thenetwork.worker.tasks import app, process_email

PROXIMITY_THRESHOLD = 0.3


def _pace_one_per_person(
    candidates: list[tuple[float, tuple[str, str], dict]],
) -> list[dict]:
    """Order candidates deterministically and cap scheduling at one per person.

    `candidates` items are (score, canonical_pair_key, payload). Ordering is
    score-descending with the pair key as a stable tiebreak, so the same state
    always yields the same picks. A person already scheduled this scan blocks
    any further candidate involving them; the skipped pair simply waits for a
    later scan (or is superseded by real activity in the meantime).
    """
    candidates.sort(key=lambda c: (-c[0], c[1]))
    engaged: set[str] = set()
    picks: list[dict] = []
    for _score, pair_key, payload in candidates:
        if pair_key[0] in engaged or pair_key[1] in engaged:
            continue
        engaged.update(pair_key)
        picks.append(payload)
    return picks


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

    with get_session() as session:
        people = session.exec(
            select(Person).where(col(Person.id).in_(person_ids))
        ).all()
        email_by_id = {p.id: p.email for p in people}

        candidates: list[tuple[float, tuple[str, str], dict]] = []
        for i, pid_a in enumerate(person_ids):
            if pid_a not in email_by_id:
                continue
            for pid_b in person_ids[i + 1:]:
                jac = list(nx.jaccard_coefficient(G, [(pid_a, pid_b)]))
                score = jac[0][2] if jac else 0.0
                if score < PROXIMITY_THRESHOLD:
                    continue
                if pair_is_suppressed(session, pid_a, pid_b):
                    continue
                pair_key: tuple[str, str] = tuple(sorted((pid_a, pid_b)))  # type: ignore[assignment]
                candidates.append(
                    (
                        score,
                        pair_key,
                        {
                            "sender_email": email_by_id[pid_a],
                            "subject": "[Proactive] Potential connection",
                            "body": (
                                f"[System trigger] You have a high-proximity match "
                                f"(score={score:.2f}). Consider reaching out."
                            ),
                            "sender_authenticated": True,
                            "is_proactive": True,
                        },
                    )
                )

    _defer_proactive_jobs(_pace_one_per_person(candidates))


@app.periodic(cron="30 * * * *", periodic_id="scan_for_matches")
@app.task()
async def scan_for_matches(timestamp: int) -> None:
    """Hourly semantic rematch: new memories against older standing notes.

    Driven by *arrivals* - memories created since roughly the last run - rather
    than by re-sweeping the whole store, so a pair only surfaces once, at the
    moment the counterpart shows up. For each recent memory, find older
    person-referencing memories about a *different* person that it now matches
    above `proactive_match_threshold`, and hand the pair to the agent. The
    dormant owner of the older note is the one re-engaged.

    Guards:
    - Pairs already connected in the graph are skipped - an introduction the
      agent already made is itself the durable dedup record.
    - The relevance floor is conservative and enforced here as well as in the
      match query: unsolicited outreach makes a false positive costly, so a
      thin keyword-overlap match (two people who merely both mention factories)
      must not surface, while a specific shared-ground match still does.
    - Pacing: at most one new candidate per person per scan, best match first,
      deterministic ordering (see `_pace_one_per_person`).
    - The trigger body carries only opaque ids + gists (SEAL-safe); raw memory
      text and real addresses never enter it.
    """
    s = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=s.proactive_rematch_lookback_minutes
    )

    with get_session() as session:
        recent = session.exec(
            select(Memory)
            .where(col(Memory.created_at) >= cutoff)
            .where(col(Memory.gist).is_not(None))
            .where(col(Memory.embedding).is_not(None))
            .order_by(col(Memory.created_at).desc())
            .limit(200)
        ).all()

        if not recent:
            return

        graph = build_graph()
        seen: set[frozenset[str]] = set()
        email_cache: dict[str, str | None] = {}

        def email_for(pid: str) -> str | None:
            if pid not in email_cache:
                person = session.get(Person, pid)
                email_cache[pid] = person.email if person else None
            return email_cache[pid]

        candidates: list[tuple[float, tuple[str, str], dict]] = []
        for arrival_mem in recent:
            arrivals = [r for r in arrival_mem.refs if r]
            if not arrivals:
                continue

            matches = match_memories(
                arrival_mem.embedding,
                session,
                limit=s.proactive_rematch_top_k,
                min_similarity=s.proactive_match_threshold,
            )
            for m in matches:
                if m.memory_id == arrival_mem.id:
                    continue
                if m.similarity < s.proactive_match_threshold:
                    continue
                standing = m.person_id  # owner of the older matched note
                if standing in arrivals:
                    continue

                for arrival in arrivals:
                    pair = frozenset((standing, arrival))
                    if (
                        pair in seen
                        or graph.has_edge(standing, arrival)
                        or pair_is_suppressed(session, standing, arrival)
                    ):
                        continue
                    seen.add(pair)

                    recipient = email_for(standing)
                    if recipient is None:
                        continue

                    body = (
                        "[System match] A new signal about one person closely "
                        "matches a standing note about another "
                        f"(similarity={m.similarity:.2f}).\n\n"
                        f"Person {standing}: {m.gist}\n"
                        f"Person {arrival}: {arrival_mem.gist}\n\n"
                        "If these two share specific, real common ground, "
                        "propose an introduction with `propose_introduction`, "
                        "using only what the gists support. If the overlap is thin "
                        "or you are unsure, do nothing."
                    )
                    pair_key: tuple[str, str] = tuple(sorted(pair))  # type: ignore[assignment]
                    candidates.append(
                        (
                            m.similarity,
                            pair_key,
                            {
                                "sender_email": recipient,
                                "subject": "[Proactive] Possible connection",
                                "body": body,
                                "sender_authenticated": True,
                                "is_proactive": True,
                            },
                        )
                    )

        _defer_proactive_jobs(_pace_one_per_person(candidates))
