"""Contracts for bounded, grouped match evidence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from thenetwork.search.match import (
    MemoryMatch,
    SealedMemoryEvidence,
    build_candidate_contexts,
    load_person_evidence,
)


def test_candidate_context_order_dedup_and_bounds_are_deterministic():
    matches = [
        MemoryMatch("p-anchor", "person-p", "seeks a climate cofounder", 0.91),
        MemoryMatch("p-anchor", "person-p", "seeks a climate cofounder", 0.90),
        MemoryMatch("q-anchor", "person-q", "builds industrial heat systems", 0.88),
        MemoryMatch("p-role", "person-p", "product leader", 0.86),
        MemoryMatch("r-anchor", "person-r", "third candidate", 0.80),
    ]
    supporting = {
        "person-p": [
            SealedMemoryEvidence("p-duplicate", " seeks  a climate cofounder "),
            SealedMemoryEvidence("p-stage", "pre-seed stage"),
        ],
        "person-q": [
            SealedMemoryEvidence("q-scope", "x" * 100),
        ],
    }

    contexts = build_candidate_contexts(
        matches,
        supporting,
        max_candidates=2,
        max_evidence_per_person=3,
        max_chars_per_person=45,
    )

    assert [context.person_id for context in contexts] == ["person-p", "person-q"]
    assert [context.similarity for context in contexts] == [0.91, 0.88]
    assert [item.memory_id for item in contexts[0].evidence] == [
        "p-anchor",
        "p-role",
        "p-stage",
    ]
    for context in contexts:
        assert len(context.evidence) <= 3
        assert sum(len(item.gist) for item in context.evidence) <= 45


def test_load_person_evidence_is_a_gist_only_bounded_projection():
    rows = [
        SimpleNamespace(memory_id="newer", person_id="person-p", gist="new gist"),
        SimpleNamespace(memory_id="older", person_id="person-p", gist="old gist"),
    ]
    session = MagicMock()
    session.exec.return_value.all.return_value = rows

    evidence = load_person_evidence(
        session,
        ["person-p", "person-p", ""],
        per_person_limit=2,
    )

    statement = str(session.exec.call_args.args[0])
    params = session.exec.call_args.kwargs["params"]
    assert "m.text" not in statement
    assert "people" not in statement.lower()
    assert params == {"person_ids": ["person-p"], "per_person_limit": 2}
    assert evidence == {
        "person-p": [
            SealedMemoryEvidence("newer", "new gist"),
            SealedMemoryEvidence("older", "old gist"),
        ]
    }
