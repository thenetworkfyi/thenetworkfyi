from __future__ import annotations

from datetime import datetime, timezone

import networkx as nx
from sqlalchemy import text

from thenetwork.db.session import get_session


RECENCY_HALF_LIFE_DAYS = 180.0


def _recency_weight(
    created_at: datetime,
    half_life_days: float = RECENCY_HALF_LIFE_DAYS,
) -> float:
    age_days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
    return 2 ** (-age_days / half_life_days)


def build_graph() -> nx.Graph:
    """Project an undirected graph from memories with >=2 refs.

    Nodes = people ids. An edge exists between two people when a memory
    references both. Edge weight = sum of recency-weighted counts across all
    memories referencing that pair. Proximity scoring is deferred until the
    graph is dense enough to be meaningful.
    """
    G: nx.Graph = nx.Graph()
    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT refs, created_at
                FROM memories
                WHERE array_length(refs, 1) >= 2
            """)
        ).fetchall()
    for row in rows:
        refs = list(row.refs)
        w = _recency_weight(row.created_at)
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                a, b = refs[i], refs[j]
                if G.has_edge(a, b):
                    G[a][b]["weight"] += w
                else:
                    G.add_edge(a, b, weight=w)
    return G


def score_proximity(
    requester_id: str, candidate_ids: list[str]
) -> dict[str, float]:
    """Return a proximity score [0, 1] for each candidate relative to requester.

    Score = Jaccard coefficient on shared graph neighbours. Returns 0 when
    requester is not in the graph. Proximity scoring is deferred until the
    graph is dense enough to be meaningful.
    """
    G = build_graph()
    if requester_id not in G:
        return {cid: 0.0 for cid in candidate_ids}
    scores: dict[str, float] = {}
    for cid in candidate_ids:
        if cid not in G:
            scores[cid] = 0.0
            continue
        jac = list(nx.jaccard_coefficient(G, [(requester_id, cid)]))
        scores[cid] = jac[0][2] if jac else 0.0
    return scores
