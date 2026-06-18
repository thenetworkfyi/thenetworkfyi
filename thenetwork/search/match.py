from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session


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
) -> list[MemoryMatch]:
    """Semantic search over person-referencing memories with a gist (SEAL-sanitized)."""
    vec_literal = '[' + ','.join(str(v) for v in query_vec) + ']'
    sql = text("""
        SELECT
            m.id          AS memory_id,
            m.refs[1]     AS person_id,
            m.gist        AS gist,
            1 - (m.embedding <=> CAST(:vec AS vector)) AS similarity
        FROM memories m
        WHERE
            m.embedding IS NOT NULL
            AND m.gist IS NOT NULL
            AND array_length(m.refs, 1) >= 1
            AND 1 - (m.embedding <=> CAST(:vec AS vector)) >= :min_sim
        ORDER BY m.embedding <=> CAST(:vec AS vector)
        LIMIT :limit
    """)
    rows = session.execute(
        sql, {"vec": vec_literal, "min_sim": min_similarity, "limit": limit}
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
