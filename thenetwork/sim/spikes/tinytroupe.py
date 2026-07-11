"""TinyTroupe go/no-go spike for email-form persona behavior.

This module deliberately keeps the first spike outside the production mail
pipeline. It verifies that a TinyPerson can be configured with action
correction and can produce repeated email-shaped turns against a mocked
Network agent before later tasks wire the real process_email path.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol


DEFAULT_PERSONA_EMAIL = "mara.vidal@example.test"
DEFAULT_AGENT_ADDRESS = "join@thenetwork.test"


class TinyPersonLike(Protocol):
    """Small subset of TinyPerson used by the spike."""

    name: str

    def listen_and_act(self, stimulus: str, *args: Any, **kwargs: Any) -> Any:
        """Receive a stimulus and return the persona's next action."""


@dataclass(frozen=True)
class SpikeTurn:
    """One persona email and one mocked-agent reply."""

    index: int
    persona_email: EmailMessage
    agent_reply: EmailMessage


@dataclass(frozen=True)
class SpikeTranscript:
    """Complete output of the TinyTroupe spike."""

    persona_name: str
    turns: tuple[SpikeTurn, ...]
    action_correction_enabled: bool


class MockNetworkAgent:
    """Deterministic stand-in for The Network during the TinyTroupe spike."""

    def __init__(self) -> None:
        self._responses = (
            "Thanks. I can remember that and look for useful overlaps.",
            "Specific constraints help. Tell me who you do not want to meet.",
            "Noted. I would only make an intro when there is a strong reason.",
            "That is enough for this spike. No real memory was written.",
        )

    def reply(self, message: EmailMessage, turn_index: int) -> EmailMessage:
        response = EmailMessage()
        response["From"] = DEFAULT_AGENT_ADDRESS
        response["To"] = message["From"] or DEFAULT_PERSONA_EMAIL
        response["Subject"] = f"Re: {message['Subject'] or 'The Network'}"
        response.set_content(self._responses[min(turn_index, len(self._responses) - 1)])
        return response


def build_difficult_persona() -> TinyPersonLike:
    """Create the TinyPerson used by the real spike run.

    TinyTroupe is an optional dev-harness dependency. Import it lazily so the
    normal test suite and worker runtime do not require it.
    """
    try:
        from tinytroupe.agent import TinyPerson
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "TinyTroupe is required for the live spike. Install it with "
            "`uv pip install 'git+https://github.com/microsoft/TinyTroupe.git'`."
        ) from exc

    person = TinyPerson("Mara Vidal")
    person.define("age", 39)
    person.define(
        "occupation",
        {
            "title": "Independent manufacturing consultant",
            "organization": "self-employed",
            "description": (
                "You advise small factories on procurement and operations. You are "
                "careful with privacy and skeptical of vague networking pitches."
            ),
        },
    )
    person.define(
        "personality",
        {
            "traits": [
                "You are terse and direct.",
                "You do not agree just to be polite.",
                "You ask for specifics before trusting a new service.",
                "You stop engaging when replies feel generic.",
            ],
        },
    )
    person.define(
        "preferences",
        {
            "interests": [
                "Resilient supply chains",
                "Industrial automation",
                "Meeting operators with concrete field experience",
            ],
            "dislikes": [
                "Warm introductions with no clear reason",
                "Being pushed to reveal client names",
                "Overly cheerful assistant copy",
            ],
        },
    )
    return person


def enable_action_correction(person: TinyPersonLike) -> bool:
    """Enable TinyTroupe action quality correction on a TinyPerson if present."""
    generator = getattr(person, "action_generator", None)
    if generator is None:
        return False

    for attr, value in (
        ("enable_quality_checks", True),
        ("quality_threshold", 6),
        ("max_attempts", 4),
        ("enable_regeneration", True),
    ):
        if hasattr(generator, attr):
            setattr(generator, attr, value)
    return bool(getattr(generator, "enable_quality_checks", False))


def _extract_text(action_result: Any) -> str:
    """Best-effort extraction of a written message from TinyTroupe actions."""
    if isinstance(action_result, str):
        return action_result.strip()
    if isinstance(action_result, Mapping):
        return _extract_text_from_mapping(action_result)
    if isinstance(action_result, Iterable):
        for item in action_result:
            text = _extract_text(item)
            if text:
                return text
    return ""


def _extract_text_from_mapping(action: Mapping[str, Any]) -> str:
    for key in ("content", "text", "message", "utterance"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested = action.get("action")
    if isinstance(nested, Mapping):
        return _extract_text_from_mapping(nested)

    return ""


def _email_from_persona(
    *,
    persona_name: str,
    body: str,
    turn_index: int,
    from_address: str,
    to_address: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{persona_name} <{from_address}>"
    msg["To"] = to_address
    msg["Subject"] = "Testing The Network"
    msg["X-Sim-Turn"] = str(turn_index + 1)
    msg.set_content(body)
    return msg


def run_spike(
    *,
    create_person: Callable[[], TinyPersonLike] = build_difficult_persona,
    agent: MockNetworkAgent | None = None,
    turns: int = 4,
    persona_email: str = DEFAULT_PERSONA_EMAIL,
    agent_address: str = DEFAULT_AGENT_ADDRESS,
) -> SpikeTranscript:
    """Run the four-turn TinyTroupe email spike against a mocked agent."""
    if turns < 1:
        raise ValueError("turns must be at least 1")

    person = create_person()
    correction_enabled = enable_action_correction(person)
    mock_agent = agent or MockNetworkAgent()
    persona_name = getattr(person, "name", "TinyPerson")

    stimulus = (
        "Write a short email to The Network. You are skeptical: explain what "
        "kind of useful introduction would be worth your time, ask one pointed "
        "question, and do not flatter the service."
    )
    transcript: list[SpikeTurn] = []

    for index in range(turns):
        action = person.listen_and_act(stimulus)
        body = _extract_text(action) or (
            "I am evaluating whether this is worth my time. What exactly do "
            "you match on, and how do you avoid vague introductions?"
        )
        persona_msg = _email_from_persona(
            persona_name=persona_name,
            body=body,
            turn_index=index,
            from_address=persona_email,
            to_address=agent_address,
        )
        reply = mock_agent.reply(persona_msg, index)
        transcript.append(
            SpikeTurn(index=index + 1, persona_email=persona_msg, agent_reply=reply)
        )
        stimulus = (
            "The Network replied by email:\n\n"
            f"{reply.get_content()}\n\n"
            "Reply in one short email. Stay skeptical, do not gush, and end "
            "the exchange naturally if you have enough information."
        )

    return SpikeTranscript(
        persona_name=persona_name,
        turns=tuple(transcript),
        action_correction_enabled=correction_enabled,
    )


def render_transcript(transcript: SpikeTranscript) -> str:
    """Render a compact text transcript for manual go/no-go review."""
    lines = [
        f"TinyTroupe spike persona: {transcript.persona_name}",
        f"Action correction enabled: {transcript.action_correction_enabled}",
        "",
    ]
    for turn in transcript.turns:
        lines.extend(
            [
                f"Turn {turn.index} persona -> agent",
                (turn.persona_email.get_content() or "").strip(),
                "",
                f"Turn {turn.index} agent -> persona",
                (turn.agent_reply.get_content() or "").strip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TinyTroupe email spike.")
    parser.add_argument("--turns", type=int, default=4)
    args = parser.parse_args()
    print(render_transcript(run_spike(turns=args.turns)), end="")


if __name__ == "__main__":
    main()
