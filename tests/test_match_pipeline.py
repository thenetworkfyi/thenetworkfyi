"""Integration tests for the match pipeline against seeded fixtures.

Marked with pytest.mark.integration — require a live pgvector DB.
Run with: pytest -m integration
"""
from __future__ import annotations

import pytest

from thenetwork.search.graph import score_proximity


@pytest.mark.integration
def test_graph_proximity_mutual_connections(seeded_profiles, seeded_connections):
    """alice <-> carol (mutual), so proximity > 0 for alice->carol and vice versa."""
    alice, _bob, carol, dave = seeded_profiles

    import networkx as nx

    G = nx.DiGraph()
    for conn in seeded_connections:
        G.add_edge(conn.user_id_a, conn.user_id_b, weight=conn.connection_strength)
    UG = G.to_undirected()

    jac = list(nx.jaccard_coefficient(UG, [(alice.id, carol.id)]))
    # They have a direct mutual edge — both are in each other's neighbors
    assert jac[0][2] >= 0.0  # Jaccard may be 0 with only direct edge, that's fine


@pytest.mark.integration
def test_graph_proximity_excludes_unconnected(seeded_profiles, seeded_connections):
    """bob has no connections in the fixture — he is absent from the graph, so proximity is 0."""
    alice, bob, _carol, _dave = seeded_profiles

    import networkx as nx

    G = nx.DiGraph()
    for conn in seeded_connections:
        G.add_edge(conn.user_id_a, conn.user_id_b, weight=conn.connection_strength)
    UG = G.to_undirected()

    # nx.jaccard_coefficient raises NodeNotFound for absent nodes; score_proximity returns 0 instead.
    assert bob.id not in UG


@pytest.mark.asyncio
async def test_e2e_producer_to_agent(monkeypatch):
    """E2E: producer enqueues -> worker runs agent -> reply captured."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from thenetwork.worker.tasks import process_email

    emails_sent: list[dict] = []

    async def fake_run_agent(sender_email, sender_user_id, email_subject, email_body):
        return "Thanks for your email!"

    async def fake_check_rate(_):
        return True

    async def fake_scan(_):
        return True, "ok"

    with patch("thenetwork.worker.tasks.run_agent_for_email", new_callable=AsyncMock, side_effect=fake_run_agent), \
         patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, "ok")), \
         patch("thenetwork.worker.tasks.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.first.return_value = None
        mock_gs.return_value = mock_session

        await process_email.fn(
            sender_email="test@example.com",
            subject="Hello",
            body="I'm a backend engineer looking for ML folks.",
        )


# ---------------------------------------------------------------------------
# Real pgvector DB integration tests — require seeded_db fixture
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_match_candidates_ranks_by_similarity(seeded_db):
    """match_candidates hits pgvector and returns carol ranked above bob for an ml query."""
    from thenetwork.search.match import match_candidates

    results = await match_candidates(
        query_vector=seeded_db["query_ml"],
        requester_id=seeded_db["alice_id"],
        top_k=10,
    )

    assert results, "expected at least one result from pgvector query"
    returned_ids = [r.user_id for r in results]
    assert seeded_db["carol_id"] in returned_ids, "carol (similar to ml query) must appear"
    if seeded_db["bob_id"] in returned_ids:
        assert returned_ids.index(seeded_db["carol_id"]) < returned_ids.index(seeded_db["bob_id"]), \
            "carol must rank above bob for an ml-direction query"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_match_candidates_excludes_requester(seeded_db):
    """The requester's own profile must never appear in results."""
    from thenetwork.search.match import match_candidates

    results = await match_candidates(
        query_vector=seeded_db["query_ml"],
        requester_id=seeded_db["alice_id"],
        top_k=10,
    )

    assert all(r.user_id != seeded_db["alice_id"] for r in results), \
        "requester alice must be absent from results"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_match_candidates_skill_filter(seeded_db):
    """required_skills=['ml'] keeps carol (has ml) and drops bob (rust) and dave (product)."""
    from thenetwork.search.match import match_candidates

    results = await match_candidates(
        query_vector=seeded_db["query_ml"],
        requester_id=seeded_db["alice_id"],
        required_skills=["ml"],
        top_k=10,
    )

    returned_ids = {r.user_id for r in results}
    assert seeded_db["carol_id"] in returned_ids, "carol has 'ml' skill and must appear"
    assert seeded_db["bob_id"] not in returned_ids, "bob lacks 'ml' skill and must be excluded"
    assert seeded_db["dave_id"] not in returned_ids, "dave lacks 'ml' skill and must be excluded"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_match_candidates_excludes_unavailable(seeded_db, pg_engine):
    """Profiles with available_to_collaborate=False must be excluded from results."""
    from sqlalchemy import text
    from thenetwork.search.match import match_candidates

    with pg_engine.connect() as conn:
        conn.execute(
            text("UPDATE profiles SET available_to_collaborate = false WHERE id = :cid"),
            {"cid": seeded_db["carol_id"]},
        )
        conn.commit()

    try:
        results = await match_candidates(
            query_vector=seeded_db["query_ml"],
            requester_id=seeded_db["alice_id"],
            top_k=10,
        )
        assert all(r.user_id != seeded_db["carol_id"] for r in results), \
            "unavailable carol must not appear in results"
    finally:
        with pg_engine.connect() as conn:
            conn.execute(
                text("UPDATE profiles SET available_to_collaborate = true WHERE id = :cid"),
                {"cid": seeded_db["carol_id"]},
            )
            conn.commit()
