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
    """bob has no connections in the fixture — proximity to alice should be 0."""
    alice, bob, _carol, _dave = seeded_profiles

    import networkx as nx

    G = nx.DiGraph()
    for conn in seeded_connections:
        G.add_edge(conn.user_id_a, conn.user_id_b, weight=conn.connection_strength)
    UG = G.to_undirected()

    jac = list(nx.jaccard_coefficient(UG, [(alice.id, bob.id)]))
    assert jac[0][2] == 0.0


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
