"""Unit tests for thenetwork.worker.proactive."""
from __future__ import annotations

from uuid import UUID

import pytest
import networkx as nx
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_session(people):
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    s.exec.return_value.all.return_value = people
    s.exec.return_value.first.return_value = None
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
    assert all(call.kwargs["sender_authenticated"] for call in mock_pe.defer.call_args_list)
    trace_ids = [call.kwargs["trace_id"] for call in mock_pe.defer.call_args_list]
    assert len(trace_ids) == len(set(trace_ids))
    assert all(str(UUID(trace_id, version=4)) == trace_id for trace_id in trace_ids)
    # the bound counterpart is the surfaced high-proximity pair's other id, and
    # never the effective sender's own id (propose_introduction pairing binding)
    call = next(c for c in mock_pe.defer.call_args_list if c.kwargs["sender_email"] == "alice@test.com")
    assert call.kwargs["proactive_candidate_id"] == "bob"


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_reads_person_emails_before_real_session_closes(seeded_db):
    """The scan snapshots ORM fields before get_session commits and expires them."""
    from thenetwork.worker.proactive import scan_for_opportunities

    graph = nx.Graph()
    graph.add_edge(seeded_db["alice_id"], seeded_db["dave_id"])
    graph.add_edge(seeded_db["bob_id"], seeded_db["dave_id"])

    with patch("thenetwork.worker.proactive.build_graph", return_value=graph), patch(
        "thenetwork.worker.proactive.process_email"
    ) as mock_process:
        await scan_for_opportunities.func(0)

    mock_process.defer.assert_called_once()
    assert mock_process.defer.call_args.kwargs["sender_email"] == "alice@test.com"


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
    s.exec.return_value.first.return_value = None
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
    assert kwargs["sender_authenticated"] is True
    assert str(UUID(kwargs["trace_id"], version=4)) == kwargs["trace_id"]
    # the bound counterpart is the newly-arrived person (Q), not the dormant
    # standing-note owner (P) who is the effective sender
    assert kwargs["proactive_candidate_id"] == "Q"


@pytest.mark.asyncio
async def test_rematch_job_reaches_agent_through_real_worker_handoff():
    """Synthetic rematch jobs authenticate their DB-resolved sender identity."""
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches
    from thenetwork.worker.tasks import process_email

    recent = [_memory("n1", ["Q"], "just started looking for a rust cofounder")]
    matches = [MemoryMatch("m1", "P", "building a rust startup, wants a cofounder", 0.72)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.process_email") as deferred:
        await scan_for_matches.func(0)

    job = deferred.defer.call_args.kwargs
    worker_session = MagicMock()
    worker_session.__enter__ = MagicMock(return_value=worker_session)
    worker_session.__exit__ = MagicMock(return_value=False)
    worker_session.get.return_value = None
    worker_session.exec.return_value.first.return_value = None

    with patch("thenetwork.worker.tasks.get_session", return_value=worker_session), \
         patch("thenetwork.worker.tasks.check_rate_limit", return_value=True) as check_rate_limit, \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, "ok")), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch("thenetwork.worker.tasks.process_consent_reply", return_value=ConsentReplyResult(handled=False)), \
         patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as run_agent:
        await process_email.func(**job)

    run_agent.assert_awaited_once()
    check_rate_limit.assert_called_once_with(
        "p@test.com", sender_authenticated=True, skip_sender_limit=True
    )
    assert run_agent.call_args.kwargs["sender_authenticated"] is True
    assert run_agent.call_args.kwargs["sender_email"] == "p@test.com"
    # the bound candidate id survives the full defer() -> process_email.func()
    # -> run_agent_for_email handoff, so propose_introduction can enforce it
    assert run_agent.call_args.kwargs["proactive_candidate_id"] == "Q"


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
    # match resolves to Q - same person as the arrival's subject
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
    """SEAL: the deferred body carries only opaque ids + gists - never a real
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


# --- pacing + relevance gate ------------------------------------------------

def _engaged_ids(defer_calls):
    """Person ids named in each deferred trigger body ("Person <id>: ...")."""
    import re

    pairs = []
    for call in defer_calls:
        pairs.append(tuple(re.findall(r"Person (\S+):", call.kwargs["body"])))
    return pairs


@pytest.mark.asyncio
async def test_rematch_gate_rejects_thin_overlap_keeps_specific_match():
    """A 0.55 thin keyword-overlap match (factory/climate) is rejected even if
    the match backend returns it; a specific 0.72 manufacturing-ML match stays."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory("mi", ["ines"], "runs a climate-adjacent factory project"),
        _memory("ms", ["samir"], "deploys ML infrastructure on factory floors"),
    ]
    per_call_matches = [
        [MemoryMatch("mn", "nora", "climate founder, industrial heat reuse", 0.55)],
        [MemoryMatch("mp", "priya", "ML platform work for factory operations", 0.72)],
    ]
    persons = {
        "ines": _person("ines", "ines@test.com"),
        "samir": _person("samir", "samir@test.com"),
        "nora": _person("nora", "nora@test.com"),
        "priya": _person("priya", "priya@test.com"),
    }

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", side_effect=per_call_matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1, "only the specific match may surface"
    kwargs = mock_pe.defer.call_args.kwargs
    assert kwargs["sender_email"] == "priya@test.com"
    for call in mock_pe.defer.call_args_list:
        assert "nora" not in call.kwargs["body"]
        assert "ines" not in call.kwargs["body"]


@pytest.mark.asyncio
async def test_rematch_schedules_at_most_one_candidate_per_person():
    """When two candidates share a person, only the higher-similarity one is
    scheduled this scan; selection is deterministic (score, then pair key)."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory("m1", ["B"], "wants ml manufacturing peers"),
        _memory("m2", ["C"], "wants ml manufacturing peers too"),
    ]
    per_call_matches = [
        [MemoryMatch("ma", "A", "runs ml in factories", 0.75)],
        [MemoryMatch("mb", "B", "wants ml manufacturing peers", 0.65)],
    ]
    persons = {p: _person(p, f"{p.lower()}@test.com") for p in ("A", "B", "C")}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", side_effect=per_call_matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1, "B may be scheduled only once per scan"
    kwargs = mock_pe.defer.call_args.kwargs
    assert kwargs["sender_email"] == "a@test.com", "the higher-similarity pair wins"


@pytest.mark.asyncio
async def test_rematch_manufacturing_cluster_does_not_burst():
    """Four mutually-matching manufacturing members yield at most two paced
    pairs per scan (each person in at most one), never the combinatorial six."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    members = ["P1", "P2", "P3", "P4"]
    recent = [
        _memory(f"m{p}", [p], f"{p} does ml for manufacturing") for p in members
    ]
    per_call_matches = [
        [
            MemoryMatch(f"s{other}", other, f"{other} does ml for manufacturing", 0.7)
            for other in members
            if other != p
        ]
        for p in members
    ]
    persons = {p: _person(p, f"{p.lower()}@test.com") for p in members}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", side_effect=per_call_matches), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count <= 2, (
        f"expected at most 2 paced pairs for 4 people, got {mock_pe.defer.call_count}"
    )
    engaged = [pid for pair in _engaged_ids(mock_pe.defer.call_args_list) for pid in pair]
    assert len(engaged) == len(set(engaged)), "a person may appear in at most one pair per scan"


@pytest.mark.asyncio
async def test_rematch_preserves_pair_suppression():
    """A previously proposed/resolved pair stays suppressed under pacing."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "wants a rust cofounder")]
    matches = [MemoryMatch("m1", "P", "rust founder", 0.9)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.pair_is_suppressed", return_value=True), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_prioritizes_zero_load_candidate_over_saturated_peers():
    """Reproduces the run-shaped scenario where four already-engaged recipients
    kept winning pacing while a strong, unengaged match (Omar-like) never got
    scheduled. All candidates here compete for the same newly-arrived person,
    so only one is paced; request-load ordering must pick the zero-load
    candidate even though the saturated candidates score as high or higher,
    without lowering the relevance floor or the per-scan one-per-person cap."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory("arrival", ["newcomer"], "ml infrastructure operator, seeking collaborators")
    ]
    saturated = ["s1", "s2", "s3", "s4"]
    per_call_matches = [
        [
            MemoryMatch("m-omar", "omar", "ml infrastructure standing note", 0.68),
            *[
                MemoryMatch(f"m-{s}", s, "ml infrastructure standing note", 0.75)
                for s in saturated
            ],
        ]
    ]
    persons = {
        "newcomer": _person("newcomer", "newcomer@test.com"),
        "omar": _person("omar", "omar@test.com"),
        **{s: _person(s, f"{s}@test.com") for s in saturated},
    }

    def fake_request_load(_session, person_id, *, since):
        return 0 if person_id in ("newcomer", "omar") else 3

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=_rematch_session(recent, persons)), \
         patch("thenetwork.worker.proactive.match_memories", side_effect=per_call_matches), \
         patch("thenetwork.worker.proactive.request_load", side_effect=fake_request_load), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1, (
        "all candidates compete for the same newcomer; only one may be paced"
    )
    kwargs = mock_pe.defer.call_args.kwargs
    assert kwargs["sender_email"] == "omar@test.com", (
        "the zero-load candidate must be scheduled ahead of similarly relevant "
        "saturated candidates, even though the saturated ones score higher"
    )


@pytest.mark.asyncio
async def test_opportunities_scan_paces_one_candidate_per_person():
    """A star graph makes every leaf pair Jaccard 1.0; pacing schedules at most
    one candidate per person instead of every qualifying pair."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    for leaf in ("a", "b", "c"):
        G.add_edge(leaf, "hub")

    people = [_person(p, f"{p}@test.com") for p in ("a", "b", "c", "hub")]

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session", return_value=_mock_session(people)), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.call_count == 1, (
        f"three qualifying pairs share persons; expected 1 paced defer, got {mock_pe.defer.call_count}"
    )


@pytest.mark.asyncio
async def test_opportunities_scan_skips_suppressed_pairs():
    """A proposed/resolved introduction pair is not re-surfaced by the graph scan."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_edge("alice", "dave")
    G.add_edge("bob", "dave")

    people = [_person(p, f"{p}@test.com") for p in ("alice", "bob", "dave")]

    with patch("thenetwork.worker.proactive.build_graph", return_value=G), \
         patch("thenetwork.worker.proactive.get_session", return_value=_mock_session(people)), \
         patch("thenetwork.worker.proactive.pair_is_suppressed", return_value=True), \
         patch("thenetwork.worker.proactive.process_email") as mock_pe:
        await scan_for_opportunities.func(0)

    assert not mock_pe.defer.called
