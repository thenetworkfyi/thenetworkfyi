"""NetworkX graph proximity scoring over network_connections."""
from __future__ import annotations

import networkx as nx
from sqlalchemy import text

from thenetwork.db.session import get_session


def _build_graph() -> nx.DiGraph:
    """Load all directed edges from DB into a NetworkX DiGraph."""
    G = nx.DiGraph()
    with get_session() as session:
        rows = session.execute(
            text("SELECT user_id_a, user_id_b, connection_strength FROM network_connections")
        ).fetchall()
    for row in rows:
        G.add_edge(row.user_id_a, row.user_id_b, weight=row.connection_strength)
    return G


def score_proximity(
    requester_id: str, candidate_ids: list[str]
) -> dict[str, float]:
    """Return a proximity score [0, 1] for each candidate relative to requester.

    Score = normalised common-neighbours count (Jaccard coefficient on the
    undirected projection), falling back to 0 if requester is not in the graph.
    """
    G = _build_graph()
    UG = G.to_undirected()

    if requester_id not in UG:
        return {cid: 0.0 for cid in candidate_ids}

    scores: dict[str, float] = {}
    for cid in candidate_ids:
        if cid not in UG:
            scores[cid] = 0.0
            continue
        pairs = [(requester_id, cid)]
        jac = list(nx.jaccard_coefficient(UG, pairs))
        scores[cid] = jac[0][2] if jac else 0.0
    return scores
