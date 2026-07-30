from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from thenetwork.search.graph import RECENCY_HALF_LIFE_DAYS

MAX_CANDIDATE_CONTEXTS = 5
MAX_EVIDENCE_GISTS_PER_PERSON = 4
MAX_EVIDENCE_CHARS_PER_PERSON = 1_200


@dataclass
class MemoryMatch:
    memory_id: str
    person_id: str
    gist: str
    similarity: float


@dataclass(frozen=True)
class SealedMemoryEvidence:
    """One PII-sanitized memory projection safe for candidate context."""

    memory_id: str
    gist: str


@dataclass(frozen=True)
class CandidateContext:
    """Bounded sealed evidence for one opaque candidate person id."""

    person_id: str
    similarity: float
    evidence: tuple[SealedMemoryEvidence, ...]


def load_person_evidence(
    session: Session,
    person_ids: list[str],
    *,
    per_person_limit: int = MAX_EVIDENCE_GISTS_PER_PERSON,
) -> dict[str, list[SealedMemoryEvidence]]:
    """Load recent gist-only evidence for opaque people, bounded in SQL.

    This is a SEAL projection: the query deliberately selects no raw memory
    text, person row, name, or address. Character bounds are applied when the
    evidence is assembled for model context.
    """
    ordered_ids = list(
        dict.fromkeys(person_id for person_id in person_ids if person_id)
    )
    evidence = {person_id: [] for person_id in ordered_ids}
    if not ordered_ids or per_person_limit <= 0:
        return evidence

    rows = session.exec(
        text("""
            WITH ranked AS (
                SELECT
                    m.id AS memory_id,
                    m.gist AS gist,
                    ref.person_id AS person_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY ref.person_id
                        ORDER BY m.created_at DESC, m.id
                    ) AS evidence_rank
                FROM memories m
                CROSS JOIN LATERAL unnest(m.refs) AS ref(person_id)
                WHERE
                    m.gist IS NOT NULL
                    AND ref.person_id = ANY(CAST(:person_ids AS text[]))
            )
            SELECT memory_id, gist, person_id
            FROM ranked
            WHERE evidence_rank <= :per_person_limit
            ORDER BY person_id, evidence_rank
        """),
        params={
            "person_ids": ordered_ids,
            "per_person_limit": per_person_limit,
        },
    ).all()
    for row in rows:
        evidence[row.person_id].append(
            SealedMemoryEvidence(memory_id=row.memory_id, gist=row.gist)
        )
    return evidence


def build_candidate_contexts(
    matches: list[MemoryMatch],
    supporting_evidence: dict[str, list[SealedMemoryEvidence]] | None = None,
    *,
    max_candidates: int = MAX_CANDIDATE_CONTEXTS,
    max_evidence_per_person: int = MAX_EVIDENCE_GISTS_PER_PERSON,
    max_chars_per_person: int = MAX_EVIDENCE_CHARS_PER_PERSON,
) -> list[CandidateContext]:
    """Group ranked matches into bounded, deterministic candidate evidence.

    Candidate order and similarity come from the first ranked retrieval hit.
    Within a candidate, ranked hit gists come first, followed by recent
    supporting gists. Exact duplicate ids or normalized gists are removed.
    """
    if max_candidates <= 0 or max_evidence_per_person <= 0 or max_chars_per_person <= 0:
        return []

    ordered_people: list[str] = []
    similarity_by_person: dict[str, float] = {}
    anchors_by_person: dict[str, list[SealedMemoryEvidence]] = {}
    for match in matches:
        if match.person_id not in similarity_by_person:
            if len(ordered_people) == max_candidates:
                continue
            ordered_people.append(match.person_id)
            similarity_by_person[match.person_id] = match.similarity
            anchors_by_person[match.person_id] = []
        if match.person_id in anchors_by_person:
            anchors_by_person[match.person_id].append(
                SealedMemoryEvidence(memory_id=match.memory_id, gist=match.gist)
            )

    supporting_evidence = supporting_evidence or {}
    contexts = []
    for person_id in ordered_people:
        bounded: list[SealedMemoryEvidence] = []
        seen_ids: set[str] = set()
        seen_gists: set[str] = set()
        chars = 0
        ordered_evidence = anchors_by_person[person_id] + supporting_evidence.get(
            person_id, []
        )
        for item in ordered_evidence:
            gist = item.gist.strip()
            normalized_gist = " ".join(gist.split()).casefold()
            if not gist or item.memory_id in seen_ids or normalized_gist in seen_gists:
                continue
            remaining = max_chars_per_person - chars
            if remaining <= 0:
                break
            bounded_gist = gist[:remaining]
            bounded.append(
                SealedMemoryEvidence(
                    memory_id=item.memory_id,
                    gist=bounded_gist,
                )
            )
            seen_ids.add(item.memory_id)
            seen_gists.add(normalized_gist)
            chars += len(bounded_gist)
            if len(bounded) == max_evidence_per_person:
                break
        if bounded:
            contexts.append(
                CandidateContext(
                    person_id=person_id,
                    similarity=similarity_by_person[person_id],
                    evidence=tuple(bounded),
                )
            )
    return contexts


def match_memories(
    query_vec: list[float],
    session: Session,
    *,
    limit: int = 10,
    min_similarity: float = 0.0,
    exclude_memory_id: str | None = None,
    sole_ref_person_id: str | None = None,
) -> list[MemoryMatch]:
    """Semantic search over person-referencing memories with a gist (SEAL-sanitized)."""
    vec_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
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
                AND (
                    CAST(:sole_ref_person_id AS text) IS NULL
                    OR m.refs = ARRAY[CAST(:sole_ref_person_id AS text)]
                )
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
    rows = session.exec(
        sql,
        params={
            "vec": vec_literal,
            "min_sim": min_similarity,
            "limit": limit,
            "exclude_id": exclude_memory_id,
            "sole_ref_person_id": sole_ref_person_id,
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
