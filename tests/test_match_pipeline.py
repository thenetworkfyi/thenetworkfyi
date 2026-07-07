"""Integration tests for the match pipeline against seeded fixtures.

Marked with pytest.mark.integration - require a live pgvector DB.
Run with: pytest -m integration
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


def _vec_str(dim0: float = 0.0, dim1: float = 0.0) -> str:
    v = [0.0] * 1536
    v[0] = dim0
    v[1] = dim1
    return "[" + ",".join(str(x) for x in v) + "]"


def _insert_memory(
    conn,
    *,
    memory_id: str,
    raw_text: str,
    emb: str,
    refs: list[str],
    gist: str | None,
    created_at_sql: str = "NOW()",
) -> None:
    refs_sql = "ARRAY[" + ",".join(f"'{r}'" for r in refs) + "]::text[]"
    conn.execute(text(f"""
        INSERT INTO memories (id, text, embedding, refs, gist, created_at)
        VALUES (:mid, :txt, CAST(:emb AS vector), {refs_sql}, :gist, {created_at_sql})
    """), {"mid": memory_id, "txt": raw_text, "emb": emb, "gist": gist})


@pytest.mark.asyncio
async def test_e2e_producer_to_agent(monkeypatch):
    """E2E: producer enqueues -> worker runs agent -> reply captured."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from thenetwork.worker.tasks import process_email

    async def fake_run_agent(
        sender_email,
        sender_user_id,
        email_subject,
        email_body,
        sender_authenticated=False,
        sender_display_name=None,
        **kwargs,
    ):
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
            sender_authenticated=True,
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
def test_match_memories_returns_only_gist_for_matching_memory(seeded_db, pg_engine):
    """A matching memory returns the sanitized gist, not the raw memory text."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    memory_id = str(uuid.uuid4())
    raw_text = "Alice Secret alice.secret@example.com is evaluating Acme"
    gist = "evaluating a company"
    with pg_engine.connect() as conn:
        _insert_memory(
            conn,
            memory_id=memory_id,
            raw_text=raw_text,
            emb=_vec_str(1.0, 0.0),
            refs=[seeded_db["alice_id"]],
            gist=gist,
        )
        conn.commit()

    try:
        with get_session() as session:
            results = match_memories(seeded_db["query_ml"], session, limit=20)
        match = next(r for r in results if r.memory_id == memory_id)
        assert match.gist == gist
        assert "alice.secret@example.com" not in match.gist
        assert "Alice Secret" not in match.gist
        assert not hasattr(match, "text")
    finally:
        with pg_engine.connect() as conn:
            conn.execute(text("DELETE FROM memories WHERE id = :id"), {"id": memory_id})
            conn.commit()


@pytest.mark.integration
def test_match_memories_recency_can_rank_fresh_over_stale(seeded_db, pg_engine):
    """Blended ranking can prefer a fresh, slightly weaker match over a stale exact match."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    stale_id = str(uuid.uuid4())
    fresh_id = str(uuid.uuid4())
    with pg_engine.connect() as conn:
        _insert_memory(
            conn,
            memory_id=stale_id,
            raw_text="stale exact ml match",
            emb=_vec_str(1.0, 0.0),
            refs=[f"stale-ref-{stale_id}"],
            gist="stale exact ml match",
            created_at_sql="NOW() - INTERVAL '720 days'",
        )
        _insert_memory(
            conn,
            memory_id=fresh_id,
            raw_text="fresh approximate ml match",
            emb=_vec_str(0.85, 0.526782687642637),
            refs=[f"fresh-ref-{fresh_id}"],
            gist="fresh approximate ml match",
        )
        conn.commit()

    try:
        with get_session() as session:
            results = match_memories(
                seeded_db["query_ml"],
                session,
                limit=50,
                min_similarity=0.5,
            )
        by_id = {r.memory_id: r for r in results}
        assert stale_id in by_id
        assert fresh_id in by_id
        assert by_id[stale_id].similarity > by_id[fresh_id].similarity

        memory_order = [r.memory_id for r in results]
        assert memory_order.index(fresh_id) < memory_order.index(stale_id)
    finally:
        with pg_engine.connect() as conn:
            conn.execute(
                text("DELETE FROM memories WHERE id = ANY(:ids)"),
                {"ids": [stale_id, fresh_id]},
            )
            conn.commit()


@pytest.mark.integration
def test_match_memories_min_similarity_filters_raw_similarity(seeded_db, pg_engine):
    """min_similarity filters semantic similarity, not recency-blended rank score."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    low_similarity_id = str(uuid.uuid4())
    with pg_engine.connect() as conn:
        _insert_memory(
            conn,
            memory_id=low_similarity_id,
            raw_text="fresh but orthogonal match",
            emb=_vec_str(0.0, 1.0),
            refs=[f"low-ref-{low_similarity_id}"],
            gist="fresh but orthogonal match",
        )
        conn.commit()

    try:
        with get_session() as session:
            results = match_memories(
                seeded_db["query_ml"],
                session,
                limit=50,
                min_similarity=0.8,
            )
        assert low_similarity_id not in {r.memory_id for r in results}
        assert all(r.similarity >= 0.8 for r in results)
    finally:
        with pg_engine.connect() as conn:
            conn.execute(text("DELETE FROM memories WHERE id = :id"), {"id": low_similarity_id})
            conn.commit()


@pytest.mark.integration
def test_match_memories_returns_one_match_per_ref(seeded_db, pg_engine):
    """A memory with refs A and B attributes the match to both refs."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

    memory_id = str(uuid.uuid4())
    ref_a = f"ref-a-{memory_id}"
    ref_b = f"ref-b-{memory_id}"
    with pg_engine.connect() as conn:
        _insert_memory(
            conn,
            memory_id=memory_id,
            raw_text="two people share an ml interest",
            emb=_vec_str(1.0, 0.0),
            refs=[ref_a, ref_b],
            gist="two people share an ml interest",
        )
        conn.commit()

    try:
        with get_session() as session:
            results = match_memories(seeded_db["query_ml"], session, limit=50)
        refs_for_memory = sorted(
            r.person_id for r in results if r.memory_id == memory_id
        )
        assert refs_for_memory == sorted([ref_a, ref_b])
    finally:
        with pg_engine.connect() as conn:
            conn.execute(text("DELETE FROM memories WHERE id = :id"), {"id": memory_id})
            conn.commit()


@pytest.mark.integration
def test_match_memories_excludes_ungisted(seeded_db, pg_engine):
    """Memories where gist IS NULL must not appear in match_memories results."""
    from thenetwork.db.session import get_session
    from thenetwork.search.match import match_memories

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
    """dave has no multi-ref memories - he must be absent from the graph."""
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
    """alice-carol share a direct edge but no common neighbors - Jaccard is 0."""
    from thenetwork.search.graph import score_proximity

    scores = score_proximity(seeded_db["alice_id"], [seeded_db["carol_id"]])
    assert scores[seeded_db["carol_id"]] == 0.0, \
        "Jaccard coefficient is 0 when two nodes share only a direct edge with no common neighbors"
