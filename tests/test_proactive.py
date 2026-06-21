"""Unit tests for thenetwork.worker.proactive."""
from __future__ import annotations

import pytest
import networkx as nx
from unittest.mock import MagicMock, patch


def _mock_session(people):
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    s.exec.return_value.all.return_value = people
    return s


def _person(pid, email):
    p = MagicMock()
    p.id = pid
    p.email = email
    return p


@pytest.mark.asyncio
async def test_scan_enqueues_high_proximity_pair():
    """Pairs sharing a common neighbor (Jaccard > 0) above threshold are deferred."""
    from thenetwork.worker.proactive import scan_for_opportunities

    # alice and bob both connect to dave → Jaccard(alice, bob) = 1.0
    G = nx.Graph()
    G.add_edge("alice", "dave")
    G.add_edge("bob", "dave")

    people = [_person("alice", "alice@test.com"), _person("bob", "bob@test.com"), _person("dave", "dave@test.com")]

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session", return_value=_mock_session(people)), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.called, "process_email.defer must be called for a high-proximity pair"
    sender_emails = {c.kwargs["sender_email"] for c in mock_pe.defer.call_args_list}
    assert "alice@test.com" in sender_emails


@pytest.mark.asyncio
async def test_scan_skips_low_proximity_pairs():
    """Direct edge with no common neighbors → Jaccard=0 → no defer."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_edge("alice", "bob")

    people = [_person("alice", "alice@test.com"), _person("bob", "bob@test.com")]

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session", return_value=_mock_session(people)), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_opportunities.func(0)

    assert not mock_pe.defer.called, "Jaccard=0 pair must not be deferred"


@pytest.mark.asyncio
async def test_scan_deduplicates_pairs():
    """Each unique (a, b) pair is enqueued at most once."""
    from thenetwork.worker.proactive import scan_for_opportunities

    # alice and bob share dave → exactly one high-proximity pair
    G = nx.Graph()
    G.add_edge("alice", "dave")
    G.add_edge("bob", "dave")

    people = [_person("alice", "alice@test.com"), _person("bob", "bob@test.com"), _person("dave", "dave@test.com")]

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session", return_value=_mock_session(people)), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.call_count == 1, f"expected 1 defer call, got {mock_pe.defer.call_count}"


@pytest.mark.asyncio
async def test_scan_early_returns_on_empty_graph():
    """Fewer than 2 nodes → return without hitting the DB or deferring."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_node("alice")

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session") as mock_gs, \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_opportunities.func(0)

    mock_gs.assert_not_called()
    assert not mock_pe.defer.called
