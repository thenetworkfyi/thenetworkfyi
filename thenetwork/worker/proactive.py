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
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import networkx as nx
from sqlmodel import col, select

from thenetwork.db.models import Memory, Person
from thenetwork.db.session import get_session
from thenetwork.search.graph import build_graph
from thenetwork.search.match import match_memories
from thenetwork.settings import get_settings
from thenetwork.worker.tasks import app, process_email

PROXIMITY_THRESHOLD = 0.3


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

    for i, pid_a in enumerate(person_ids):
        if pid_a not in email_by_id:
            continue
        for pid_b in person_ids[i + 1:]:
            jac = list(nx.jaccard_coefficient(G, [(pid_a, pid_b)]))
            score = jac[0][2] if jac else 0.0
            if score >= PROXIMITY_THRESHOLD:
                process_email.defer(
                    sender_email=email_by_id[pid_a],
                    subject="[Proactive] Potential connection",
                    body=(
                        f"[System trigger] You have a high-proximity match "
                        f"(score={score:.2f}). Consider reaching out."
                    ),
                )


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
    - The similarity floor is conservative: unsolicited outreach makes a false
      positive costly, so this path takes a floor where interactive `search`
      deliberately does not.
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
                standing = m.person_id  # owner of the older matched note
                if standing in arrivals:
                    continue

                for arrival in arrivals:
                    pair = frozenset((standing, arrival))
                    if pair in seen or graph.has_edge(standing, arrival):
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
                        "introduce them - dispatch_email to each, mentioning "
                        "only what the gists support. If the overlap is thin "
                        "or you are unsure, do nothing."
                    )
                    process_email.defer(
                        sender_email=recipient,
                        subject="[Proactive] Possible connection",
                        body=body,
                    )
