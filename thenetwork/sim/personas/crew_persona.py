"""CrewAI Persona Agent and Task construction for simulation turns."""

from __future__ import annotations

from typing import Any

from crewai import Agent, LLM, Task

from thenetwork.sim.personas.crew_mail_tool import SimMailboxTool
from thenetwork.sim.personas.persona import PersonaConfig
from thenetwork.sim.personas.response import PASS_SENTINEL, _is_pass_sentinel


def build_persona_agent(
    config: PersonaConfig,
    llm: LLM,
    memory: bool = False,
    verbose: bool = False,
    **kwargs: Any,
) -> Agent:
    """Build a CrewAI Agent instance representing a simulation persona.

    Configures role, goal, backstory, and memory from PersonaConfig.
    """
    role = f"{config.name} <{config.email}>"
    goal = config.goal
    backstory = (
        f"You are {config.name} <{config.email}>, a real person corresponding "
        f"by email with a networking service called The Network ({config.agent_address}). "
        f"Your stop condition: {config.stop_condition}"
    )

    agent_kwargs: dict[str, Any] = {
        "role": role,
        "goal": goal,
        "backstory": backstory,
        "llm": llm,
        "memory": memory,
        "verbose": verbose,
        "allow_delegation": False,
    }
    agent_kwargs.update(kwargs)
    return Agent(**agent_kwargs)


def build_persona_turn_task(
    agent: Agent,
    stimulus: str,
    tick: int | None = None,
    **kwargs: Any,
) -> Task:
    """Build a CrewAI Task for a single persona turn given a stimulus string."""
    tick_prefix = f"Current simulation tick: {tick}\n\n" if tick is not None else ""
    description = (
        f"{tick_prefix}"
        f"Stimulus / Inbox state:\n{stimulus}\n\n"
        "Instructions:\n"
        "Reply with ONLY the plain-text body of the next email you would send to The Network - "
        "no subject line, no greeting-card fluff, no markdown. Keep it under 120 words, natural and specific.\n"
        "When replying to an introduction request containing an `[intro:...]` token, put exactly one decision word - "
        "YES, NO, or REVOKE - on the first line, and copy the complete `[intro:...]` token exactly as received onto the second line.\n"
        f"If your stop condition is met, or you have nothing genuinely new to say, reply with exactly {PASS_SENTINEL} and nothing else."
    )
    task_kwargs: dict[str, Any] = {
        "description": description,
        "expected_output": "Plain-text email body response, or PASS sentinel.",
        "agent": agent,
    }
    task_kwargs.update(kwargs)
    return Task(**task_kwargs)


def extract_persona_response(output: Any) -> dict[str, str]:
    """Extract and parse persona turn response text from CrewAI task output."""
    if isinstance(output, str):
        text = output.strip()
    elif hasattr(output, "raw") and isinstance(output.raw, str):
        text = output.raw.strip()
    elif hasattr(output, "result") and isinstance(output.result, str):
        text = output.result.strip()
    else:
        raise TypeError("CrewAI persona output must contain a string response")

    if _is_pass_sentinel(text):
        return {"content": ""}
    return {"content": text}


class CrewTinyPerson:
    """A conversational persona backed by a CrewAI agent."""

    def __init__(self, config: PersonaConfig, llm: LLM) -> None:
        self.name = config.name
        self.config = config
        self.agent = build_persona_agent(config, llm)
        self.llm = llm
        self.mailbox_tool: SimMailboxTool | None = None
        self._tick: int | None = None

    def prepare_turn(self, *, post_office: Any, tick: int, reply_to: Any) -> None:
        """Bind the CrewAI mailbox capability to the current runtime turn."""
        self.mailbox_tool = SimMailboxTool(
            config=self.config,
            post_office=post_office,
            tick=tick,
            reply_to=reply_to,
            allow_send=False,
        )
        self._tick = tick
        self.agent.tools = [self.mailbox_tool]

    async def alisten_and_act(self, stimulus: str) -> dict[str, str]:
        task = build_persona_turn_task(self.agent, stimulus, tick=self._tick)
        output = await task.execute_async()
        return extract_persona_response(output)

    def listen_and_act(
        self, stimulus: str, *args: Any, **kwargs: Any
    ) -> dict[str, str]:
        task = build_persona_turn_task(self.agent, stimulus)
        output = task.execute_sync()
        return extract_persona_response(output)
