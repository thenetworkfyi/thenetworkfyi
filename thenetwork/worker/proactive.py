"""Proactive outreach trigger: periodic Procrastinate task.

Scans the memory graph for high-proximity person pairs and enqueues agent jobs
to evaluate introductions. The trigger only identifies opportunities; the agent
decides whether and how to introduce.
"""
from __future__ import annotations

import networkx as nx

from thenetwork.search.graph import build_graph
from thenetwork.db.session import get_session
from thenetwork.db.models import Person
from thenetwork.worker.tasks import app, process_email
from sqlmodel import col, select

PROXIMITY_THRESHOLD = 0.3


@app.periodic(cron="0 * * * *")
@app.task()
async def scan_for_opportunities(_timestamp: int) -> None:
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
