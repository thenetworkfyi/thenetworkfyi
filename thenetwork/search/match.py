"""match_candidates: direct pgvector similarity + NetworkX proximity.

Returns a ranked list of opaque user IDs with non-identifying rationale —
never names, emails, or raw bios (THE SEAL: minimal disclosure).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from thenetwork.db.session import get_session
from thenetwork.search.graph import score_proximity

SIMILARITY_THRESHOLD = 0.0  # postprocessor: filter nodes below this cosine similarity


@dataclass
class MatchResult:
    user_id: str            # opaque — no PII
    similarity: float
    mutual_connections: float
    combined_score: float
    skill_overlap: list[str]


async def match_candidates(
    query_vector: list[float],
    requester_id: str,
    required_skills: list[str] | None = None,
    top_k: int = 10,
) -> list[MatchResult]:
    """Pgvector cosine-similarity on profiles.intent_vector + NetworkX proximity blend.

    Only opaque IDs and non-identifying metadata are returned (minimal disclosure).
    The LLM never sees names, emails, or raw bios of other users.
    """
    # pgvector accepts Python list formatted as "[0.1, 0.2, ...]"
    vec_literal = str(query_vector)

    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT id, skills, available_to_collaborate,
                       1 - (intent_vector <=> CAST(:vec AS vector)) AS similarity
                FROM profiles
                WHERE intent_vector IS NOT NULL
                ORDER BY intent_vector <=> CAST(:vec AS vector)
                LIMIT :limit
            """),
            {"vec": vec_literal, "limit": top_k * 2},
        ).fetchall()

    # Postprocessor: filter requester, unavailable, low-similarity, and skill mismatches
    candidates: list[tuple[str, float, list[str]]] = []
    for row in rows:
        uid = row.id
        if uid == requester_id:
            continue
        if not row.available_to_collaborate:
            continue
        sim = float(row.similarity)
        if sim < SIMILARITY_THRESHOLD:
            continue
        node_skills: list[str] = list(row.skills or [])
        overlap = [sk for sk in (required_skills or []) if sk in node_skills]
        if required_skills and not overlap:
            continue
        candidates.append((uid, sim, overlap))

    if not candidates:
        return []

    candidate_ids = [c[0] for c in candidates]
    proximity = score_proximity(requester_id, candidate_ids)

    results: list[MatchResult] = []
    for uid, sim, overlap in candidates:
        prox = proximity.get(uid, 0.0)
        combined = 0.7 * sim + 0.3 * prox
        results.append(
            MatchResult(
                user_id=uid,
                similarity=sim,
                mutual_connections=prox,
                combined_score=combined,
                skill_overlap=overlap,
            )
        )

    results.sort(key=lambda r: r.combined_score, reverse=True)
    return results[:top_k]
