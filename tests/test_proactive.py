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


# --- scan_for_matches (semantic rematch) -----------------------------------

def _memory(mid, refs, gist):
    m = MagicMock()
    m.id = mid
    m.refs = refs
    m.gist = gist
    m.embedding = [0.0]  # match_memories is mocked, so the value is unused
    return m


def _rematch_session(recent, persons):
    """A mock session that serves recent memories from .exec().all() and
    resolves Person rows from .get(Person, pid)."""
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    s.exec.return_value.all.return_value = recent
    s.get.side_effect = lambda _model, pid: persons.get(pid)
    return s


@pytest.mark.asyncio
async def test_rematch_enqueues_new_match_against_standing_note():
    """A recent memory matching an older note about a different person defers
    a job that re-engages the dormant owner of the older note."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "just started looking for a rust cofounder")]
    matches = [MemoryMatch("m1", "P", "building a rust startup, wants a cofounder", 0.72)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1
    kwargs = mock_pe.defer.call_args.kwargs
    # the dormant standing-note owner (P) is the one re-engaged
    assert kwargs["sender_email"] == "p@test.com"
    assert "P" in kwargs["body"] and "Q" in kwargs["body"]
    assert "rust startup" in kwargs["body"]  # P's gist
    assert "rust cofounder" in kwargs["body"]  # Q's gist


@pytest.mark.asyncio
async def test_rematch_skips_already_connected_pair():
    """If the graph already has an edge between the pair, don't re-introduce."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "wants a rust cofounder")]
    matches = [MemoryMatch("m1", "P", "rust founder", 0.9)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    G = nx.Graph()
    G.add_edge("P", "Q")  # already introduced

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_skips_self_match():
    """A match whose person is the same as the arrival's subject is not a pair."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "wants a rust cofounder")]
    # match resolves to Q — same person as the arrival's subject
    matches = [MemoryMatch("m0", "Q", "wants a rust cofounder", 0.99)]
    persons = {"Q": _person("Q", "q@test.com")}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_early_returns_when_no_recent_memories():
    """No memories in the lookback window → return before building the graph."""
    from thenetwork.worker.proactive import scan_for_matches

    with patch("thenetwork.worker.proactive.build_graph") as mock_bg, \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session([], {})), \
         patch("thenetwork.worker.proactive.match_memories") as mock_mm, \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    mock_bg.assert_not_called()
    mock_mm.assert_not_called()
    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_trigger_body_carries_no_raw_pii():
    """SEAL: the deferred body carries only opaque ids + gists — never a real
    address or raw memory text for either party."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "wants a rust cofounder")]
    matches = [MemoryMatch("m1", "P", "rust founder seeking cofounder", 0.8)]
    p = _person("P", "p@test.com")
    p.name = "Priya Raw-Name"
    q = _person("Q", "q@test.com")
    q.name = "Quentin Raw-Name"
    persons = {"P": p, "Q": q}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    body = mock_pe.defer.call_args.kwargs["body"]
    assert "p@test.com" not in body
    assert "q@test.com" not in body
    assert "Raw-Name" not in body
