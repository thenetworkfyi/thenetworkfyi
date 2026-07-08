from __future__ import annotations

from dataclasses import dataclass

import pytest

from thenetwork.sim.tinytroupe_spike import (
    MockNetworkAgent,
    render_transcript,
    run_spike,
)


@dataclass
class FakeActionGenerator:
    enable_quality_checks: bool = False
    quality_threshold: int = 0
    max_attempts: int = 0
    enable_regeneration: bool = False


class FakeTinyPerson:
    name = "Mara Vidal"

    def __init__(self) -> None:
        self.action_generator = FakeActionGenerator()
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        return {
            "action": {
                "type": "TALK",
                "content": f"I am skeptical turn {len(self.stimuli)}. What is the concrete match reason?",
            }
        }


def test_spike_runs_four_email_turns_with_action_correction_enabled():
    person = FakeTinyPerson()

    transcript = run_spike(
        create_person=lambda: person,
        agent=MockNetworkAgent(),
        turns=4,
    )

    assert transcript.action_correction_enabled is True
    assert person.action_generator.enable_quality_checks is True
    assert person.action_generator.quality_threshold == 6
    assert person.action_generator.max_attempts == 4
    assert person.action_generator.enable_regeneration is True
    assert len(transcript.turns) == 4
    assert len(person.stimuli) == 4
    assert transcript.turns[0].persona_email["From"] == "Mara Vidal <mara.vidal@example.test>"
    assert transcript.turns[0].persona_email["To"] == "join@thenetwork.test"
    assert transcript.turns[0].persona_email["X-Sim-Turn"] == "1"
    assert "skeptical turn 1" in transcript.turns[0].persona_email.get_content()
    assert transcript.turns[0].agent_reply["Subject"] == "Re: Testing The Network"


def test_spike_feeds_mocked_agent_replies_back_to_persona():
    person = FakeTinyPerson()

    run_spike(create_person=lambda: person, turns=2)

    assert "Write a short email to The Network" in person.stimuli[0]
    assert "The Network replied by email" in person.stimuli[1]
    assert "Thanks. I can remember that" in person.stimuli[1]


def test_render_transcript_is_compact_and_reviewable():
    transcript = run_spike(create_person=FakeTinyPerson, turns=1)

    rendered = render_transcript(transcript)

    assert "TinyTroupe spike persona: Mara Vidal" in rendered
    assert "Action correction enabled: True" in rendered
    assert "Turn 1 persona -> agent" in rendered
    assert "Turn 1 agent -> persona" in rendered


def test_spike_requires_positive_turn_count():
    with pytest.raises(ValueError, match="turns must be at least 1"):
        run_spike(create_person=FakeTinyPerson, turns=0)

