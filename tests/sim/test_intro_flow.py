from __future__ import annotations

import json
import mailbox
from pathlib import Path

import pytest

from thenetwork.sim.intro_flow import run_intro_flow_sim


@pytest.mark.integration
async def test_real_process_intro_flow_records_consent_reveal_and_revocation(tmp_path):
    artifacts = await run_intro_flow_sim(runs_dir=tmp_path)

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    states = [
        event["status"]
        for event in events
        if event["event"] == "sim.introduction_state"
    ]
    assert states == ["proposed", "one_consented", "introduced", "revoked"]

    tier1 = next(event for event in events if event["event"] == "sim.score.tier1")
    assert tier1["passed"] is True

    reproposal = next(
        event for event in events
        if event["event"] == "sim.introduction_reproposal"
    )
    assert reproposal == {
        "blocked": True,
        "event": "sim.introduction_reproposal",
        "reason": "revoked",
        "status": "suppressed",
    }

    completed = events[-1]
    assert completed["event"] == "sim.run_completed"
    assert completed["final_consent_state"] == "revoked"
    assert completed["reproposal_blocked"] is True
    assert completed["tier1_passed"] is True

    box = mailbox.mbox(artifacts.mbox_path)
    try:
        messages = list(box)
    finally:
        box.close()
    group_messages = [
        message for message in messages
        if message.get("Subject") == "Your introduction"
    ]
    assert len(group_messages) == 1
    assert {
        address.strip()
        for address in str(group_messages[0]["To"]).split(",")
    } == {
        "alice.intro@example.test",
        "bob.intro@example.test",
    }

    audit_events = [
        json.loads(line) for line in artifacts.audit_path.read_text().splitlines()
    ]
    assert any(
        event["event"] == "agent.tool.completed"
        and event["tool_name"] == "propose_introduction"
        for event in audit_events
    )
    assert sum(
        event["event"] == "introduction.consent_transition"
        for event in audit_events
    ) >= 4


def test_intro_flow_documentation_names_artifacts_and_command():
    docs = Path("docs/development.md").read_text()

    assert "uv run sim intro-flow" in docs
    assert "postgresql-16-pgvector" in docs
    assert "events.jsonl" in docs
    assert "audit.jsonl" in docs
