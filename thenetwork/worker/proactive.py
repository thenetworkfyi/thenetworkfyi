"""Proactive outreach trigger: periodic Procrastinate task.

Scans profiles for high-proximity pairs and enqueues agent jobs to evaluate
introductions. The trigger only identifies opportunities; the agent decides
whether and how to introduce. This is a Procrastinate periodic task, not
an email-driven flow.
"""
from __future__ import annotations

import procrastinate

from thenetwork.search.graph import score_proximity
from thenetwork.db.session import get_session
from thenetwork.db.models import Profile
from thenetwork.worker.tasks import app, process_email
from sqlmodel import select

PROXIMITY_THRESHOLD = 0.3


@app.periodic(cron="0 * * * *")
@app.task()
async def scan_for_opportunities(_timestamp: int) -> None:
    """Hourly scan: find profile pairs with high vector proximity, enqueue agent jobs."""
    with get_session() as session:
        profiles = session.exec(
            select(Profile).where(
                Profile.available_to_collaborate == True,  # noqa: E712
                Profile.intent_vector != None,  # noqa: E711
            )
        ).all()

    if len(profiles) < 2:
        return

    profile_ids = [p.id for p in profiles]

    # For each profile, check proximity to all others
    already_triggered: set[frozenset] = set()
    with app.open():
        for profile in profiles:
            others = [pid for pid in profile_ids if pid != profile.id]
            scores = score_proximity(profile.id, others)
            for other_id, score in scores.items():
                pair = frozenset([profile.id, other_id])
                if score >= PROXIMITY_THRESHOLD and pair not in already_triggered:
                    already_triggered.add(pair)
                    # Enqueue an agent job using the profile's own email as sender
                    process_email.defer(
                        sender_email=profile.email,
                        subject="[Proactive] Potential connection",
                        body=(
                            f"[System trigger] You have a high-proximity match "
                            f"(score={score:.2f}). Consider reaching out."
                        ),
                    )
