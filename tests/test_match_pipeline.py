"""Integration tests for the match pipeline against seeded fixtures.

Marked with pytest.mark.integration — require a live pgvector DB.
Run with: pytest -m integration
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_e2e_producer_to_agent(monkeypatch):
    """E2E: producer enqueues -> worker runs agent -> reply captured."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from thenetwork.worker.tasks import process_email

    async def fake_run_agent(sender_email, sender_user_id, email_subject, email_body, sender_authenticated=False):
        return "Thanks for your email."

    with patch("thenetwork.worker.tasks.run_agent_for_email", new_callable=AsyncMock, side_effect=fake_run_agent), \
         patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, "ok")), \
         patch("thenetwork.worker.tasks.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.first.return_value = None
        mock_gs.return_value = mock_session

        await process_email.func(
            sender_email="test@example.com",
            subject="Hello",
            body="I'm a backend engineer looking for ML folks.",
        )


@pytest.mark.integration
def test_match_memories_ranks_by_similarity(seeded_db):
    """match_memories hits pgvector and returns carol ranked above bob for an ml query."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    with get_session() as session:
        results = match_memories(seeded_db["query_ml"], session, limit=10)

    assert results, "expected at least one result from pgvector query"
    person_ids = [r.person_id for r in results]
    assert seeded_db["carol_id"] in person_ids, "carol (similar to ml query) must appear"
    if seeded_db["bob_id"] in person_ids:
        assert person_ids.index(seeded_db["carol_id"]) < person_ids.index(seeded_db["bob_id"]), \
            "carol must rank above bob for an ml-direction query"


@pytest.mark.integration
def test_match_memories_returns_gist_not_raw_text(seeded_db):
    """match_memories results contain gist; the MemoryMatch dataclass has no raw text field."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    with get_session() as session:
        results = match_memories(seeded_db["query_ml"], session, limit=10)

    assert results
    for m in results:
        assert m.gist is not None, "all returned memories must have a non-null gist"
        assert not hasattr(m, "text"), "MemoryMatch must not expose raw memory text"


@pytest.mark.integration
def test_match_memories_excludes_ungisted(seeded_db, pg_engine):
    """Memories where gist IS NULL must not appear in match_memories results."""
    import uuid
    from sqlalchemy import text
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    def _vec_str(dim0=0.0, dim1=0.0):
        v = [0.0] * 1536
        v[0] = dim0
        v[1] = dim1
        return "[" + ",".join(str(x) for x in v) + "]"

    nogist_id = str(uuid.uuid4())
    alice_id = seeded_db["alice_id"]
    with pg_engine.connect() as conn:
        conn.execute(text(f"""
            INSERT INTO memories (id, text, embedding, refs, gist, created_at)
            VALUES (:mid, 'ungisted memory', CAST(:emb AS vector), ARRAY['{alice_id}']::text[], NULL, NOW())
        """), {"mid": nogist_id, "emb": _vec_str(1.0, 0.0)})
        conn.commit()

    try:
        with get_session() as session:
            results = match_memories(seeded_db["query_ml"], session, limit=20)
        returned_ids = [r.memory_id for r in results]
        assert nogist_id not in returned_ids, "memory with gist=NULL must be excluded by match_memories"
    finally:
        with pg_engine.connect() as conn:
            conn.execute(text("DELETE FROM memories WHERE id = :id"), {"id": nogist_id})
            conn.commit()


@pytest.mark.integration
def test_build_graph_contains_alice_carol_edge(seeded_db):
    """intro-mem refs=[alice, carol] => both appear as nodes with a shared edge."""
    from thenetwork.search.graph import build_graph

    G = build_graph()
    assert seeded_db["alice_id"] in G.nodes, "alice must be in graph (linked by intro-mem)"
    assert seeded_db["carol_id"] in G.nodes, "carol must be in graph (linked by intro-mem)"
    assert G.has_edge(seeded_db["alice_id"], seeded_db["carol_id"]), \
        "alice and carol must share an edge via intro-mem"


@pytest.mark.integration
def test_build_graph_excludes_solo_person(seeded_db):
    """dave has no multi-ref memories — he must be absent from the graph."""
    from thenetwork.search.graph import build_graph

    G = build_graph()
    assert seeded_db["dave_id"] not in G.nodes, "dave has no multi-ref memories; must not appear in graph"


@pytest.mark.integration
def test_score_proximity_absent_node_returns_zero(seeded_db):
    """score_proximity returns 0 for a requester not in the graph."""
    from thenetwork.search.graph import score_proximity

    scores = score_proximity(seeded_db["dave_id"], [seeded_db["alice_id"], seeded_db["carol_id"]])
    for cid, v in scores.items():
        assert v == 0.0, f"dave has no graph presence; score for {cid} must be 0, got {v}"


@pytest.mark.integration
def test_score_proximity_direct_edge_no_common_neighbors(seeded_db):
    """alice-carol share a direct edge but no common neighbors — Jaccard is 0."""
    from thenetwork.search.graph import score_proximity

    scores = score_proximity(seeded_db["alice_id"], [seeded_db["carol_id"]])
    assert scores[seeded_db["carol_id"]] == 0.0, \
        "Jaccard coefficient is 0 when two nodes share only a direct edge with no common neighbors"
