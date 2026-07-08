"""Authored persona population and schedule controls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thenetwork.sim.persona import PersonaConfig


@dataclass(frozen=True)
class ScheduledEvent:
    tick: int
    persona_email: str
    text: str
    kind: str = "intervention"
    payload: Any | None = None


@dataclass(frozen=True)
class MechanicalInterruption:
    persona_email: str
    start_tick: int
    end_tick: int | None = None
    kind: str = "silence"

    def active(self, tick: int) -> bool:
        return tick >= self.start_tick and (
            self.end_tick is None or tick <= self.end_tick
        )


@dataclass(frozen=True)
class PopulationPersona:
    config: PersonaConfig
    opening_body: str
    scheduled_events: tuple[ScheduledEvent, ...] = ()
    interruptions: tuple[MechanicalInterruption, ...] = ()


@dataclass(frozen=True)
class SimSchedule:
    events: tuple[ScheduledEvent, ...] = ()
    interruptions: tuple[MechanicalInterruption, ...] = ()

    @classmethod
    def from_population(cls, population: tuple[PopulationPersona, ...]) -> "SimSchedule":
        events: list[ScheduledEvent] = []
        interruptions: list[MechanicalInterruption] = []
        for persona in population:
            events.extend(persona.scheduled_events)
            interruptions.extend(persona.interruptions)
        return cls(events=tuple(events), interruptions=tuple(interruptions))

    def events_for(self, persona: PersonaConfig, tick: int) -> tuple[ScheduledEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.tick == tick and event.persona_email == persona.email
        )

    def is_interrupted(self, persona: PersonaConfig, tick: int) -> bool:
        return any(
            interruption.persona_email == persona.email and interruption.active(tick)
            for interruption in self.interruptions
        )


def default_population(agent_address: str = "join@thenetwork.test") -> tuple[PopulationPersona, ...]:
    rows = (
        ("Priya Shah", "priya.sim@example.test", "Find applied ML infrastructure peers in manufacturing operations.", "I run ML platform work for factory operations and want peers with production scars."),
        ("Samir Vale", "samir.sim@example.test", "Meet operators deploying ML systems in factory environments.", "I help deploy ML infrastructure on factory floors and want grounded operator feedback."),
        ("Nora Chen", "nora.sim@example.test", "Find climate founders working on industrial heat reuse.", "I am exploring industrial heat reuse and want people who understand plant constraints."),
        ("Mateo Ruiz", "mateo.sim@example.test", "Meet designers turning dense technical workflows into usable internal tools.", "I design internal tools for lab operations and want to compare notes on adoption."),
        ("Lena Okafor", "lena.sim@example.test", "Find legal operators handling open-source AI procurement.", "I work on procurement and legal ops for open-source AI and want practical peers."),
        ("Arun Mehta", "arun.sim@example.test", "Meet people building local-first collaboration software.", "I am building local-first collaboration tools and want others wrestling with sync."),
        ("Elise Laurent", "elise.sim@example.test", "Find museum technologists working on provenance and digital archives.", "I work on digital archives and provenance systems for museums."),
        ("Jon Bell", "jon.sim@example.test", "Meet founders who sell to municipal utilities.", "I sell software to municipal utilities and want to meet people with similar cycles."),
        ("Mara Vidal", "mara.sim@example.test", "Find manufacturing consultants with strong privacy boundaries.", "I advise small factories and only want specific introductions with clear reasons."),
        ("Theo Anders", "theo.sim@example.test", "Meet researchers studying simulated users and evaluation harnesses.", "I study simulated-user evaluation and want others building practical harnesses."),
    )
    population = tuple(
        PopulationPersona(
            config=PersonaConfig(
                name=name,
                email=email,
                goal=goal,
                stop_condition="Stop once your intent is registered or the thread feels generic.",
                message_budget=3,
                agent_address=agent_address,
            ),
            opening_body=opening,
        )
        for name, email, goal, opening in rows
    )
    return (
        *population[:2],
        _with_event(
            population[2],
            ScheduledEvent(
                tick=3,
                persona_email=population[2].config.email,
                text="You just accepted a pilot with a cement plant in Lisbon.",
            ),
        ),
        *population[3:8],
        _with_interruption(
            population[8],
            MechanicalInterruption(
                persona_email=population[8].config.email,
                start_tick=2,
                end_tick=4,
                kind="silence",
            ),
        ),
        _with_interruption(
            population[9],
            MechanicalInterruption(
                persona_email=population[9].config.email,
                start_tick=5,
                kind="dormancy",
            ),
        ),
    )


def _with_event(persona: PopulationPersona, event: ScheduledEvent) -> PopulationPersona:
    return PopulationPersona(
        config=persona.config,
        opening_body=persona.opening_body,
        scheduled_events=persona.scheduled_events + (event,),
        interruptions=persona.interruptions,
    )


def _with_interruption(
    persona: PopulationPersona,
    interruption: MechanicalInterruption,
) -> PopulationPersona:
    return PopulationPersona(
        config=persona.config,
        opening_body=persona.opening_body,
        scheduled_events=persona.scheduled_events,
        interruptions=persona.interruptions + (interruption,),
    )

