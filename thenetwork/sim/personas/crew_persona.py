"""CrewAI Persona Agent and Task construction for simulation turns."""

from __future__ import annotations

from typing import Any

from crewai import Agent, LLM

from thenetwork.sim.personas.persona import PersonaConfig


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
