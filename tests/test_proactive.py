"""Unit tests for thenetwork.worker.proactive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
import networkx as nx
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _unlimited_daily_token_budget():
    """Both scans call the real get_settings()/check_daily_token_budget, which
    would otherwise hit an unreachable DB in this unit-test environment. The
    budget-exhaustion path itself is covered by dedicated tests below."""
    with patch(
        "thenetwork.worker.proactive.check_daily_token_budget", return_value=True
    ) as mock_check:
        yield mock_check


def _mock_session(people):
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)

    def execute(statement):
        result = MagicMock()
        query = str(statement)
        result.all.return_value = (
            []
            if "proactive_surfaces" in query or "introduction_consents" in query
            else people
        )
        result.first.return_value = None
        return result

    s.exec.side_effect = execute
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

    people = [
        _person("alice", "alice@test.com"),
        _person("bob", "bob@test.com"),
        _person("dave", "dave@test.com"),
    ]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.called, (
        "process_email.defer must be called for a high-proximity pair"
    )
    sender_emails = {c.kwargs["sender_email"] for c in mock_pe.defer.call_args_list}
    assert "alice@test.com" in sender_emails
    assert all(
        call.kwargs["sender_authenticated"] for call in mock_pe.defer.call_args_list
    )
    trace_ids = [call.kwargs["trace_id"] for call in mock_pe.defer.call_args_list]
    assert len(trace_ids) == len(set(trace_ids))
    assert all(str(UUID(trace_id, version=4)) == trace_id for trace_id in trace_ids)
    # the bound counterpart is the surfaced high-proximity pair's other id, and
    # never the effective sender's own id (propose_introduction pairing binding)
    call = next(
        c
        for c in mock_pe.defer.call_args_list
        if c.kwargs["sender_email"] == "alice@test.com"
    )
    assert call.kwargs["proactive_candidate_id"] == "bob"
    # the trigger body labels who the agent acts for and which id to pass,
    # so the model does not guess and hand back the sender's own id
    body = call.kwargs["body"]
    assert "acting for person alice" in body
    assert "other_person_id=bob" in body
    assert "never the id of the person you are acting for" in body


@pytest.mark.asyncio
async def test_scan_records_network_density_from_the_same_graph_build():
    """Density must come from the scan's own build_graph() call, not a second one."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_edge("alice", "dave")
    G.add_edge("bob", "dave")

    people = [
        _person("alice", "alice@test.com"),
        _person("bob", "bob@test.com"),
        _person("dave", "dave@test.com"),
    ]

    with (
        patch(
            "thenetwork.worker.proactive.build_graph", return_value=G
        ) as mock_build_graph,
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch("thenetwork.worker.proactive.process_email"),
        patch(
            "thenetwork.worker.proactive.record_network_density"
        ) as mock_record_density,
    ):
        await scan_for_opportunities.func(0)

    mock_build_graph.assert_called_once_with()
    mock_record_density.assert_called_once_with(avg_degree=2 * 2 / 3)


@pytest.mark.asyncio
async def test_scan_records_zero_density_for_an_empty_graph():
    from thenetwork.worker.proactive import scan_for_opportunities

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
        patch(
            "thenetwork.worker.proactive.record_network_density"
        ) as mock_record_density,
    ):
        await scan_for_opportunities.func(0)

    mock_record_density.assert_called_once_with(avg_degree=0.0)
    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_scan_skips_low_proximity_pairs():
    """Direct edge with no common neighbors → Jaccard=0 → no defer."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_edge("alice", "bob")

    people = [_person("alice", "alice@test.com"), _person("bob", "bob@test.com")]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
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

    people = [
        _person("alice", "alice@test.com"),
        _person("bob", "bob@test.com"),
        _person("dave", "dave@test.com"),
    ]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.call_count == 1, (
        f"expected 1 defer call, got {mock_pe.defer.call_count}"
    )


@pytest.mark.asyncio
async def test_opportunities_scan_drops_candidates_silently_when_budget_exhausted():
    """Over budget: no defer call, and the pair is still marked surfaced (so
    it rotates to the next scan) rather than the agent burning more spend."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_edge("alice", "dave")
    G.add_edge("bob", "dave")

    people = [
        _person("alice", "alice@test.com"),
        _person("bob", "bob@test.com"),
        _person("dave", "dave@test.com"),
    ]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch(
            "thenetwork.worker.proactive.check_daily_token_budget",
            return_value=False,
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_opportunities.func(0)

    assert not mock_pe.defer.called, "over-budget candidates must not be deferred"


@pytest.mark.asyncio
async def test_rematch_scan_drops_candidates_silently_when_budget_exhausted():
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "just started looking for a rust cofounder")]
    matches = [
        MemoryMatch("m1", "P", "building a rust startup, wants a cofounder", 0.72)
    ]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch(
            "thenetwork.worker.proactive.check_daily_token_budget",
            return_value=False,
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called, "over-budget candidates must not be deferred"


@pytest.mark.asyncio
async def test_scan_early_returns_on_empty_graph():
    """Fewer than 2 nodes → return without hitting the DB or deferring."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_node("alice")

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch("thenetwork.worker.proactive.get_session") as mock_gs,
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
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

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=graph),
        patch("thenetwork.worker.proactive.process_email") as mock_process,
    ):
        await scan_for_opportunities.func(0)

    mock_process.defer.assert_called_once()
    assert mock_process.defer.call_args.kwargs["sender_email"] == "alice@test.com"


# --- scan_for_matches (semantic rematch) -----------------------------------


def _memory(mid, refs, gist, *, created_at=None):
    m = MagicMock()
    m.id = mid
    m.refs = refs
    m.gist = gist
    m.embedding = [0.0]  # match_memories is mocked, so the value is unused
    m.created_at = created_at or datetime.now(timezone.utc)
    return m


def _rematch_session(recent, persons, *, consent_history=()):
    """A mock session that serves recent memories from .exec().all() and
    resolves Person rows from .get(Person, pid)."""
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)

    def execute(statement):
        result = MagicMock()
        query = str(statement)
        if "memories" in query:
            result.all.return_value = recent
        elif "introduction_consents.person_a_consented" in query:
            result.all.return_value = consent_history
        else:
            result.all.return_value = []
        result.first.return_value = None
        return result

    s.exec.side_effect = execute
    s.get.side_effect = lambda _model, pid: persons.get(pid)
    return s


@pytest.mark.asyncio
async def test_rematch_enqueues_new_match_against_standing_note():
    """A standing note defers a job for its unengaged owner."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "just started looking for a rust cofounder")]
    matches = [
        MemoryMatch("m1", "P", "building a rust startup, wants a cofounder", 0.72)
    ]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1
    kwargs = mock_pe.defer.call_args.kwargs
    assert kwargs["sender_email"] == "q@test.com"
    assert "P" in kwargs["body"] and "Q" in kwargs["body"]
    assert "rust startup" in kwargs["body"]  # P's gist
    assert "rust cofounder" in kwargs["body"]  # Q's gist
    assert kwargs["sender_authenticated"] is True
    assert str(UUID(kwargs["trace_id"], version=4)) == kwargs["trace_id"]
    assert kwargs["proactive_candidate_id"] == "P"
    # the body labels the recipient's role and names the counterpart id to
    # pass, so the model does not offer the sender's own id back
    assert "acting for person Q" in kwargs["body"]
    assert "other_person_id=P" in kwargs["body"]
    assert "never pass their id" in kwargs["body"]


@pytest.mark.asyncio
async def test_rematch_groups_bounded_supporting_gists_for_each_opaque_person():
    from thenetwork.search.match import (
        MAX_EVIDENCE_CHARS_PER_PERSON,
        MAX_EVIDENCE_GISTS_PER_PERSON,
        MemoryMatch,
    )
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory("q-intent", ["Q"], "seeks a climate hardware cofounder"),
        _memory("q-stage", ["Q"], "pre-seed with a validated industrial pilot"),
        _memory("p-skill", ["P"], "builds industrial heat recovery systems"),
        _memory("p-intent", ["P"], "wants to join an early climate hardware team"),
    ]
    matches = [
        MemoryMatch("p-anchor", "P", "climate hardware engineer", 0.84),
    ]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    body = mock_pe.defer.call_args.kwargs["body"]
    assert "Person Q evidence:" in body
    assert "seeks a climate hardware cofounder" in body
    assert "pre-seed with a validated industrial pilot" in body
    assert "Person P evidence:" in body
    assert "builds industrial heat recovery systems" in body
    assert "wants to join an early climate hardware team" in body
    assert "retrieval signal, not a fit score" in body

    for person_id, next_person_id in (("Q", "P"), ("P", None)):
        section = body.split(f"Person {person_id} evidence:\n", 1)[1]
        if next_person_id is not None:
            section = section.split(f"\n\nPerson {next_person_id} evidence:", 1)[0]
        else:
            section = section.split("\n\nYou are acting", 1)[0]
        gists = [line.removeprefix("- ") for line in section.splitlines()]
        assert len(gists) <= MAX_EVIDENCE_GISTS_PER_PERSON
        assert sum(len(gist) for gist in gists) <= MAX_EVIDENCE_CHARS_PER_PERSON


@pytest.mark.asyncio
async def test_rematch_job_reaches_agent_through_real_worker_handoff():
    """Synthetic rematch jobs authenticate their DB-resolved sender identity."""
    from thenetwork.introductions import ConsentReplyResult
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches
    from thenetwork.worker.tasks import process_email

    recent = [_memory("n1", ["Q"], "just started looking for a rust cofounder")]
    matches = [
        MemoryMatch("m1", "P", "building a rust startup, wants a cofounder", 0.72)
    ]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as deferred,
    ):
        await scan_for_matches.func(0)

    job = deferred.defer.call_args.kwargs
    worker_session = MagicMock()
    worker_session.__enter__ = MagicMock(return_value=worker_session)
    worker_session.__exit__ = MagicMock(return_value=False)
    worker_session.get.return_value = None
    worker_session.exec.return_value.first.return_value = None

    with (
        patch("thenetwork.worker.tasks.get_session", return_value=worker_session),
        patch(
            "thenetwork.worker.tasks.check_rate_limit", return_value=True
        ) as check_rate_limit,
        patch("thenetwork.worker.tasks.check_daily_token_budget", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=ConsentReplyResult(handled=False),
        ),
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as run_agent,
    ):
        await process_email.func(**job)

    run_agent.assert_awaited_once()
    check_rate_limit.assert_called_once_with(
        "q@test.com", sender_authenticated=True, skip_sender_limit=True
    )
    assert run_agent.call_args.kwargs["sender_authenticated"] is True
    assert run_agent.call_args.kwargs["sender_email"] == "q@test.com"
    # the bound candidate id survives the full defer() -> process_email.func()
    # -> run_agent_for_email handoff, so propose_introduction can enforce it
    assert run_agent.call_args.kwargs["proactive_candidate_id"] == "P"


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

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
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

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_early_returns_when_no_recent_memories():
    """No memories in the lookback window → return before building the graph."""
    from thenetwork.worker.proactive import scan_for_matches

    with (
        patch("thenetwork.worker.proactive.build_graph") as mock_bg,
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session([], {}),
        ),
        patch("thenetwork.worker.proactive.match_memories") as mock_mm,
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
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

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    body = mock_pe.defer.call_args.kwargs["body"]
    assert "p@test.com" not in body
    assert "q@test.com" not in body
    assert "Raw-Name" not in body


# --- relevance gate ---------------------------------------------------------


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

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch(
            "thenetwork.worker.proactive.match_memories", side_effect=per_call_matches
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1, "only the specific match may surface"
    kwargs = mock_pe.defer.call_args.kwargs
    assert kwargs["sender_email"] == "samir@test.com"
    for call in mock_pe.defer.call_args_list:
        assert "nora" not in call.kwargs["body"]
        assert "ines" not in call.kwargs["body"]


@pytest.mark.asyncio
async def test_rematch_surfaces_urgent_active_sender_below_static_floor():
    """Recent write velocity plus a closing window lowers the floor to 0.50."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    now = datetime.now(timezone.utc)
    recent = [
        _memory(
            "q-window",
            ["Q"],
            "in town for three days and open to an imperfect founder match",
            created_at=now - timedelta(hours=4),
        ),
        _memory(
            "q-followup",
            ["Q"],
            "actively looking for local founders to meet",
            created_at=now - timedelta(hours=1),
        ),
    ]
    matches = [MemoryMatch("m-p", "P", "open to meeting visiting founders", 0.50)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch(
            "thenetwork.worker.proactive.match_memories", return_value=matches
        ) as mock_match,
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 1
    assert all(
        call.kwargs["min_similarity"] == pytest.approx(0.45)
        for call in mock_match.call_args_list
    )


@pytest.mark.asyncio
async def test_rematch_uses_counterpart_receptiveness_for_floor_and_ordering():
    """A receptive counterpart clears a floor that a decline-heavy one does not."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    now = datetime.now(timezone.utc)
    recent = [
        _memory(
            "q-window",
            ["Q"],
            "in town for three days and open to an imperfect founder match",
            created_at=now - timedelta(hours=4),
        ),
        _memory(
            "q-followup",
            ["Q"],
            "actively looking for local founders to meet",
            created_at=now - timedelta(hours=1),
        ),
    ]
    matches = [
        MemoryMatch("m-r", "receptive", "open to visiting founders", 0.49),
        MemoryMatch("m-d", "decliner", "sometimes meets founders", 0.51),
    ]
    persons = {
        person_id: _person(person_id, f"{person_id}@test.com")
        for person_id in ("Q", "receptive", "decliner")
    }
    consent_history = [
        SimpleNamespace(
            person_a_id="receptive",
            person_b_id="prior-match",
            person_a_consented=True,
            person_b_consented=True,
            status="introduced",
        ),
        SimpleNamespace(
            person_a_id="decliner",
            person_b_id="prior-decline",
            person_a_consented=False,
            person_b_consented=False,
            status="declined",
        ),
    ]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(
                recent,
                persons,
                consent_history=consent_history,
            ),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    mock_pe.defer.assert_called_once()
    assert mock_pe.defer.call_args.kwargs["proactive_candidate_id"] == "receptive"


def test_receptiveness_adjustment_is_bounded_and_cold_start_is_neutral():
    from thenetwork.worker.proactive import (
        MAX_RECEPTIVENESS_ADJUSTMENT,
        _receptiveness_adjustments,
    )

    histories = [
        SimpleNamespace(
            person_a_id="open",
            person_b_id="decline-heavy",
            person_a_consented=True,
            person_b_consented=False,
            status="declined",
        )
        for _ in range(20)
    ]

    adjustments = _receptiveness_adjustments(histories)

    assert adjustments["open"] == MAX_RECEPTIVENESS_ADJUSTMENT
    assert adjustments["decline-heavy"] == -MAX_RECEPTIVENESS_ADJUSTMENT
    assert adjustments.get("cold-start", 0.0) == 0.0


@pytest.mark.asyncio
async def test_rematch_keeps_dormant_sender_at_configured_floor():
    """An old standing note does not admit the same 0.50 candidate."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory(
            "q-old",
            ["Q"],
            "generally open to meeting founders",
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
    ]
    matches = [MemoryMatch("m-p", "P", "open to meeting founders", 0.50)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_shortens_surface_cooldown_for_recently_active_sender():
    """A recent sender uses the six-hour pair rotation, not the global day."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory("q-1", ["Q"], "looking for robotics peers"),
        _memory("q-2", ["Q"], "available to meet robotics founders"),
    ]
    matches = [MemoryMatch("m-p", "P", "robotics founder seeking peers", 0.70)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}
    pair = ("P", "Q")

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch(
            "thenetwork.worker.proactive.recently_surfaced_pairs",
            side_effect=[{pair}, set()],
        ) as mock_recent_pairs,
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert mock_recent_pairs.call_count == 2
    cooldown_delta = (
        mock_recent_pairs.call_args_list[1].kwargs["since"]
        - mock_recent_pairs.call_args_list[0].kwargs["since"]
    )
    assert cooldown_delta == timedelta(hours=18)
    assert mock_pe.defer.call_count == 1


@pytest.mark.asyncio
async def test_rematch_orders_candidates_by_score_then_pair_key():
    """Counterpart receptiveness breaks close scores before the pair key."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [
        _memory("m1", ["B"], "wants ml manufacturing peers"),
        _memory("m2", ["C"], "wants ml manufacturing peers too"),
    ]
    per_call_matches = [
        [MemoryMatch("ma", "A", "runs ml in factories", 0.75)],
        [MemoryMatch("mb", "B", "wants ml manufacturing peers", 0.72)],
    ]
    persons = {p: _person(p, f"{p.lower()}@test.com") for p in ("A", "B", "C")}
    consent_history = [
        SimpleNamespace(
            person_a_id="B",
            person_b_id="prior-match",
            person_a_consented=True,
            person_b_consented=True,
            status="introduced",
        )
    ]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(
                recent,
                persons,
                consent_history=consent_history,
            ),
        ),
        patch(
            "thenetwork.worker.proactive.match_memories", side_effect=per_call_matches
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert [
        call.kwargs["proactive_candidate_id"] for call in mock_pe.defer.call_args_list
    ] == ["B", "A"]


@pytest.mark.asyncio
async def test_rematch_surfaces_each_eligible_pair_once():
    """Four mutually matching members yield their six canonical pairs once."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    members = ["P1", "P2", "P3", "P4"]
    recent = [_memory(f"m{p}", [p], f"{p} does ml for manufacturing") for p in members]
    per_call_matches = [
        [
            MemoryMatch(f"s{other}", other, f"{other} does ml for manufacturing", 0.7)
            for other in members
            if other != p
        ]
        for p in members
    ]
    persons = {p: _person(p, f"{p.lower()}@test.com") for p in members}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch(
            "thenetwork.worker.proactive.match_memories", side_effect=per_call_matches
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert mock_pe.defer.call_count == 6


@pytest.mark.asyncio
async def test_rematch_preserves_pair_suppression():
    """A previously proposed/resolved pair stays suppressed."""
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = [_memory("n1", ["Q"], "wants a rust cofounder")]
    matches = [MemoryMatch("m1", "P", "rust founder", 0.9)]
    persons = {"P": _person("P", "p@test.com"), "Q": _person("Q", "q@test.com")}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(recent, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch("thenetwork.worker.proactive.pair_is_suppressed", return_value=True),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    assert not mock_pe.defer.called


@pytest.mark.asyncio
async def test_rematch_tries_next_best_counterpart_after_declined_pair_cools_down():
    """A declined first-choice pair does not strand an unengaged person.

    On a later sweep after the proactive-surface cooldown, the declined pair
    remains suppressed by its own 90-day cooldown and the next-best eligible
    counterpart is deferred instead.
    """
    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    memories = [_memory("m-q", ["Q"], "seeking a Rust cofounder")]
    matches = [
        MemoryMatch("m-p", "P", "first-choice Rust founder", 0.9),
        MemoryMatch("m-r", "R", "second-choice Rust founder", 0.8),
    ]
    persons = {
        person_id: _person(person_id, f"{person_id.lower()}@test.com")
        for person_id in ("P", "Q", "R")
    }

    def suppressed(_session, person_a, person_b, **_kwargs):
        return {person_a, person_b} == {"P", "Q"}

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_rematch_session(memories, persons),
        ),
        patch("thenetwork.worker.proactive.match_memories", return_value=matches),
        patch(
            "thenetwork.worker.proactive.pair_is_suppressed",
            side_effect=suppressed,
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_matches.func(0)

    mock_pe.defer.assert_called_once()
    assert mock_pe.defer.call_args.kwargs["proactive_candidate_id"] == "R"


async def test_opportunities_scan_orders_all_qualifying_pairs():
    """A star graph schedules every qualifying pair in stable score order."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    for leaf in ("a", "b", "c"):
        G.add_edge(leaf, "hub")

    people = [_person(p, f"{p}@test.com") for p in ("a", "b", "c", "hub")]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.call_count == 3


@pytest.mark.asyncio
async def test_opportunities_scan_rotates_recently_surfaced_pairs():
    """A recent no-action surface is skipped so the next pair gets a turn."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    for leaf in ("a", "b", "c"):
        G.add_edge(leaf, "hub")
    people = [_person(p, f"{p}@test.com") for p in ("a", "b", "c", "hub")]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch(
            "thenetwork.worker.proactive.recently_surfaced_pairs",
            return_value={("a", "b")},
        ),
        patch("thenetwork.worker.proactive.mark_pairs_surfaced") as mark_surfaced,
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_opportunities.func(0)

    assert mock_pe.defer.call_count == 2
    assert {
        call.kwargs["proactive_candidate_id"] for call in mock_pe.defer.call_args_list
    } == {"c"}
    assert mark_surfaced.call_args.args[1] == {("a", "c"), ("b", "c")}


@pytest.mark.asyncio
async def test_opportunities_scan_skips_suppressed_pairs():
    """A proposed/resolved introduction pair is not re-surfaced by the graph scan."""
    from thenetwork.worker.proactive import scan_for_opportunities

    G = nx.Graph()
    G.add_edge("alice", "dave")
    G.add_edge("bob", "dave")

    people = [_person(p, f"{p}@test.com") for p in ("alice", "bob", "dave")]

    with (
        patch("thenetwork.worker.proactive.build_graph", return_value=G),
        patch(
            "thenetwork.worker.proactive.get_session",
            return_value=_mock_session(people),
        ),
        patch("thenetwork.worker.proactive.pair_is_suppressed", return_value=True),
        patch("thenetwork.worker.proactive.process_email") as mock_pe,
    ):
        await scan_for_opportunities.func(0)

    assert not mock_pe.defer.called
