from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from thenetwork.search.graph import RECENCY_HALF_LIFE_DAYS


@dataclass
class MemoryMatch:
    memory_id: str
    person_id: str
    gist: str
    similarity: float


def match_memories(
    query_vec: list[float],
    session: Session,
    *,
    limit: int = 10,
    min_similarity: float = 0.0,
    exclude_memory_id: str | None = None,
) -> list[MemoryMatch]:
    """Semantic search over person-referencing memories with a gist (SEAL-sanitized)."""
    vec_literal = '[' + ','.join(str(v) for v in query_vec) + ']'
    sql = text("""
        WITH candidates AS (
            SELECT
                m.id AS memory_id,
                m.refs AS refs,
                m.gist AS gist,
                1 - (m.embedding <=> CAST(:vec AS vector)) AS similarity,
                POWER(
                    2.0,
                    -(
                        EXTRACT(EPOCH FROM (NOW() - m.created_at)) / 86400.0
                    ) / CAST(:recency_half_life_days AS double precision)
                ) AS recency
            FROM memories m
            WHERE
                m.embedding IS NOT NULL
                AND m.gist IS NOT NULL
                AND array_length(m.refs, 1) >= 1
        )
        SELECT
            c.memory_id AS memory_id,
            ref.person_id AS person_id,
            c.gist AS gist,
            c.similarity AS similarity
        FROM candidates c
        CROSS JOIN LATERAL unnest(c.refs) AS ref(person_id)
        WHERE
            c.similarity >= :min_sim
            AND (
                CAST(:exclude_id AS text) IS NULL
                OR c.memory_id != CAST(:exclude_id AS text)
            )
        ORDER BY
            ((c.similarity + c.recency) / 2.0) DESC,
            c.similarity DESC,
            c.memory_id,
            ref.person_id
        LIMIT :limit
    """)
    rows = session.execute(
        sql,
        {
            "vec": vec_literal,
            "min_sim": min_similarity,
            "limit": limit,
            "exclude_id": exclude_memory_id,
            "recency_half_life_days": RECENCY_HALF_LIFE_DAYS,
        },
    ).fetchall()
    return [
        MemoryMatch(
            memory_id=row.memory_id,
            person_id=row.person_id,
            gist=row.gist,
            similarity=float(row.similarity),
        )
        for row in rows
    ]
