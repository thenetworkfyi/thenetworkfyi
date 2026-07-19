from __future__ import annotations

import json
import mailbox
from pathlib import Path

import pytest

from thenetwork.sim.intro_flow import run_intro_flow_sim


@pytest.mark.integration
async def test_real_process_intro_flow_records_consent_handoff_and_revocation(tmp_path):
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
    assert events.index(tier1) > max(
        index
        for index, event in enumerate(events)
        if event.get("event") == "sim.introduction_state"
        and event.get("status") == "revoked"
    )

    reproposal = next(
        event for event in events if event["event"] == "sim.introduction_reproposal"
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
    assert completed["relay_bidirectional"] is True
    assert completed["revoked_relay_blocked"] is True
    assert completed["reproposal_blocked"] is True
    assert completed["tier1_passed"] is True

    box = mailbox.mbox(artifacts.raw_mbox_path)
    try:
        messages = list(box)
    finally:
        box.close()
    introduction_messages = [
        message for message in messages if message.get("Subject") == "Your introduction"
    ]
    assert len(introduction_messages) == 2
    assert {str(message["To"]) for message in introduction_messages} == {
        "alice.intro@example.test",
        "bob.intro@example.test",
    }
    proxy_addresses = {str(message["Reply-To"]) for message in introduction_messages}
    assert len(proxy_addresses) == 1
    (proxy_address,) = proxy_addresses
    assert proxy_address.startswith("hidden-")
    assert proxy_address.endswith("@relay.thenetwork.test")
    assert {str(message["From"]) for message in introduction_messages} == {
        f"The Network <{proxy_address}>"
    }

    relay_events = [event for event in events if event["event"] == "sim.relay_delivery"]
    assert relay_events == [
        {
            "delivered": True,
            "direction": "alice_to_bob",
            "event": "sim.relay_delivery",
        },
        {
            "delivered": True,
            "direction": "bob_to_alice",
            "event": "sim.relay_delivery",
        },
        {
            "delivered": False,
            "direction": "alice_to_bob_after_revoke",
            "event": "sim.relay_delivery",
        },
    ]

    audit_events = [
        json.loads(line) for line in artifacts.audit_path.read_text().splitlines()
    ]
    assert any(
        event["event"] == "agent.tool.completed"
        and event["tool_name"] == "propose_introduction"
        for event in audit_events
    )
    assert (
        sum(
            event["event"] == "introduction.consent_transition"
            for event in audit_events
        )
        >= 4
    )


def test_intro_flow_documentation_names_artifacts_and_command():
    docs = Path("docs/development.md").read_text()

    assert "uv run sim intro-flow" in docs
    assert "pgvector/pgvector:pg17" in docs
    assert "events.jsonl" in docs
    assert "audit.jsonl" in docs
