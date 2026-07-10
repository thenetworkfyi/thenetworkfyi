"""LLM-driven simulated persona for conversational sim runs."""
from __future__ import annotations

from typing import Any

from pydantic_ai import Agent

from thenetwork.sim.persona import PersonaConfig


PASS_SENTINEL = "PASS"

_PERSONA_PROMPT = """\
You are {name} <{email}>, a real person corresponding by email with a
networking service called The Network ({agent_address}).

Your goal: {goal}
Your stop condition: {stop_condition}

Each user message describes the current simulation tick. It may include life
events that just happened to you and any replies The Network has sent you.

Reply with ONLY the plain-text body of the next email you would send to The
Network - no subject line, no greeting-card fluff, no markdown. Keep it under
120 words, natural and specific. React to what The Network actually said: give
follow-up details it asked for, accept or decline offered introductions, and
share new information from events. Never repeat an earlier email of yours.

When replying to an introduction request that contains an `[intro:...]` token,
put exactly one decision word - YES, NO, or REVOKE - on the first line. Copy
the complete `[intro:...]` token exactly as received onto the second line; do
not alter, shorten, or invent it. Your goal decides which decision word to use
and overrides any suggestion in the message about whether to accept, decline,
or revoke.

If your stop condition is met, or you have nothing genuinely new to say this
tick, reply with exactly {pass_sentinel} and nothing else.
"""


class LLMTinyPerson:
    """A conversational persona backed by a pydantic-ai agent.

    Keeps per-persona message history across ticks so each email builds on
    the conversation so far. Replies with the PASS sentinel are translated
    to an empty action, which `TinyPersonEmailAdapter.anext_email` treats as
    declining to send this tick.
    """

    def __init__(self, config: PersonaConfig, model: Any) -> None:
        self.name = config.name
        self._agent: Agent[None, str] = Agent(
            model,
            system_prompt=_PERSONA_PROMPT.format(
                name=config.name,
                email=config.email,
                agent_address=config.agent_address,
                goal=config.goal,
                stop_condition=config.stop_condition,
                pass_sentinel=PASS_SENTINEL,
            ),
        )
        self._history: Any = None

    def listen_and_act(self, stimulus: str, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("LLMTinyPerson is async-only; use alisten_and_act")

    async def alisten_and_act(self, stimulus: str) -> dict[str, str]:
        result = await self._agent.run(stimulus, message_history=self._history)
        self._history = result.all_messages()
        text = result.output.strip()
        if text.strip(".! ").upper() == PASS_SENTINEL:
            return {"content": ""}
        return {"content": text}
