"""TinyPerson email adapter for the simulation harness."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Protocol


class TinyPersonLike(Protocol):
    name: str

    def listen_and_act(self, stimulus: str, *args: Any, **kwargs: Any) -> Any:
        """Receive a stimulus and return the persona's next action."""


@dataclass(frozen=True)
class PersonaConfig:
    """Harness-specific persona configuration."""

    name: str
    email: str
    goal: str
    stop_condition: str
    message_budget: int = 4
    agent_address: str = "join@thenetwork.test"


class TinyPersonEmailAdapter:
    """Thin adapter from TinyPerson actions to outbound EmailMessage objects."""

    def __init__(self, person: TinyPersonLike, config: PersonaConfig) -> None:
        self.person = person
        self.config = config
        self.messages_sent = 0

    @property
    def exhausted(self) -> bool:
        return self.messages_sent >= self.config.message_budget

    def next_email(
        self,
        stimulus: str,
        *,
        tick: int,
        subject: str = "The Network",
        fallback_body: Callable[[PersonaConfig], str] | None = None,
    ) -> EmailMessage | None:
        if self.exhausted:
            return None

        action = self.person.listen_and_act(stimulus)
        body = extract_action_text(action)
        if not body and fallback_body is not None:
            body = fallback_body(self.config)
        if not body:
            body = (
                f"I am {self.config.name}. {self.config.goal} "
                f"My stop condition is: {self.config.stop_condition}"
            )

        msg = EmailMessage()
        msg["From"] = f"{self.config.name} <{self.config.email}>"
        msg["To"] = self.config.agent_address
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg["X-Sim-Tick"] = str(tick)
        msg["X-Sim-Direction"] = "persona->agent"
        msg["X-Sim-Persona"] = self.config.name
        msg.set_content(body.strip())
        self.messages_sent += 1
        return msg


def extract_action_text(action_result: Any) -> str:
    """Best-effort extraction of written text from TinyTroupe action results."""
    if isinstance(action_result, str):
        return action_result.strip()
    if isinstance(action_result, Mapping):
        return _extract_text_from_mapping(action_result)
    if isinstance(action_result, Iterable):
        for item in action_result:
            text = extract_action_text(item)
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

