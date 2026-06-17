"""match_candidates: LlamaIndex PGVectorStore retrieval merged with NetworkX proximity.

Returns a ranked list of opaque user IDs with non-identifying rationale —
never names, emails, or raw bios (THE SEAL: minimal disclosure).
"""
from __future__ import annotations

from dataclasses import dataclass

from llama_index.core import VectorStoreIndex
from llama_index.core.schema import NodeWithScore
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.postgres import PGVectorStore

from thenetwork.search.graph import score_proximity
from thenetwork.settings import get_settings


@dataclass
class MatchResult:
    user_id: str            # opaque — no PII
    similarity: float
    mutual_connections: float
    combined_score: float
    skill_overlap: list[str]


def _make_vector_store() -> PGVectorStore:
    s = get_settings()
    url = s.database_url.replace("postgresql+psycopg://", "postgresql://")
    return PGVectorStore.from_params(
        connection_string=url,
        table_name="profiles",
        embed_dim=1536,
        hybrid_search=False,
    )


async def match_candidates(
    query_vector: list[float],
    requester_id: str,
    required_skills: list[str] | None = None,
    top_k: int = 10,
) -> list[MatchResult]:
    """Vector-search the profiles table, merge NetworkX proximity, return ranked results.

    Only opaque IDs and non-identifying metadata are returned (minimal disclosure).
    The LLM never sees names, emails, or raw bios of other users.
    """
    s = get_settings()
    store = _make_vector_store()
    embed_model = OpenAIEmbedding(model=s.embed_model, api_key=s.openai_api_key)

    index = VectorStoreIndex.from_vector_store(vector_store=store, embed_model=embed_model)
    retriever = index.as_retriever(similarity_top_k=top_k * 2)

    # Query with the pre-computed intent vector
    nodes: list[NodeWithScore] = await retriever.aretrieve_from_embedding(query_vector)

    # Filter: exclude requester, apply skill filter if requested
    candidates: list[tuple[str, float, list[str]]] = []
    for n in nodes:
        meta = n.node.metadata or {}
        uid = meta.get("id")
        if not uid or uid == requester_id:
            continue
        if meta.get("available_to_collaborate") is False:
            continue
        node_skills: list[str] = meta.get("skills") or []
        overlap = [sk for sk in (required_skills or []) if sk in node_skills]
        if required_skills and not overlap:
            continue
        candidates.append((uid, n.score or 0.0, overlap))

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
