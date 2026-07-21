"""SEAL-safe semantic projection over active events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session


@dataclass(frozen=True)
class EventMatch:
    event_id: str
    gist: str
    expires_at: datetime
    similarity: float


def match_events(
    query_vec: list[float],
    session: Session,
    *,
    limit: int = 10,
    min_similarity: float = 0.0,
) -> list[EventMatch]:
    """Return active event gists and lifecycle fields, never raw/owner data.

    The SQL projection is the privacy chokepoint. ``events.text`` and
    ``events.submitter_id`` are deliberately absent from both the SELECT and
    returned type so a hijacked caller cannot steer a runtime disclosure
    branch toward either field.
    """
    vec_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
    sql = text("""
        SELECT
            e.id AS event_id,
            e.gist AS gist,
            e.expires_at AS expires_at,
            1 - (e.embedding <=> CAST(:vec AS vector)) AS similarity
        FROM events e
        WHERE
            e.embedding IS NOT NULL
            AND e.gist IS NOT NULL
            AND e.cancelled_at IS NULL
            AND e.expires_at > NOW()
            AND 1 - (e.embedding <=> CAST(:vec AS vector)) >= :min_sim
        ORDER BY similarity DESC, e.id
        LIMIT :limit
    """)
    rows = session.exec(
        sql,
        params={"vec": vec_literal, "min_sim": min_similarity, "limit": limit},
    ).fetchall()
    return [
        EventMatch(
            event_id=row.event_id,
            gist=row.gist,
            expires_at=row.expires_at,
            similarity=float(row.similarity),
        )
        for row in rows
    ]
