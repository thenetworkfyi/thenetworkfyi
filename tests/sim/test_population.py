from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.db.models import Memory
from thenetwork.sim.scoring.scoring import MailFacts, score_memory_expectations
from thenetwork.sim.run.loop import SimTickLoop
from thenetwork.sim.personas.persona import TinyPersonEmailAdapter
from thenetwork.sim.personas.population import (
    DEFAULT_EXPECTATIONS,
    EVENT_ATTENDEE_EMAIL,
    EVENT_ORGANIZER_EMAIL,
    PETRA_EMAIL,
    SimSchedule,
    default_population,
)


class RecordingTinyPerson:
    def __init__(self, body: str) -> None:
        self.name = "Recording"
        self.body = body
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        return {"content": self.body}


class QualifyingPetra:
    """Reveal Petra's concrete interest only after a useful follow-up."""

    name = "Petra"

    def __init__(self) -> None:
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        if "You received a reply:" not in stimulus:
            return {
                "content": (
                    "I am interested in archival science and data management, but I am "
                    "still figuring out what kind of connection would be useful."
                )
            }
        if "which part of archival science" in stimulus.lower():
            return {
                "content": (
                    "I focus on provenance systems for museum archives and would value "
                    "peers who have worked on collection metadata."
                )
            }
        return {"content": ""}


def test_default_population_has_authored_personas_and_schedule():
    population = default_population(agent_address="join@example.test")

    assert len(population) == 19
    assert len({persona.config.email for persona in population}) == len(population)
    assert all(persona.opening_body for persona in population)

    original = population[:10]
    assert [persona.config.name for persona in original] == [
        "Priya Shah",
        "Samir Vale",
        "Nora Chen",
        "Mateo Ruiz",
        "Lena Okafor",
        "Arun Mehta",
        "Elise Laurent",
        "Jon Bell",
        "Mara Vidal",
        "Theo Anders",
    ]
    assert [persona.config.email for persona in original] == [
        "priya.sim@example.test",
        "samir.sim@example.test",
        "nora.sim@example.test",
        "mateo.sim@example.test",
        "lena.sim@example.test",
        "arun.sim@example.test",
        "elise.sim@example.test",
        "jon.sim@example.test",
        "mara.sim@example.test",
        "theo.sim@example.test",
    ]
    assert [persona.config.goal for persona in original] == [
        "Find applied ML infrastructure peers in manufacturing operations.",
        "Meet operators deploying ML systems in factory environments.",
        "Find climate founders working on industrial heat reuse.",
        "Meet designers turning dense technical workflows into usable internal tools.",
        "Find legal operators handling open-source AI procurement.",
        "Meet people building local-first collaboration software.",
        "Find museum technologists working on provenance and digital archives.",
        "Meet founders who sell to municipal utilities.",
        "Find manufacturing consultants with strong privacy boundaries.",
        "Meet researchers studying simulated users and evaluation harnesses.",
    ]
    assert [persona.opening_body for persona in original] == [
        "I run ML platform work for factory operations and want peers with production scars.",
        "I help deploy ML infrastructure on factory floors and want grounded operator feedback.",
        "I am exploring industrial heat reuse and want people who understand plant constraints.",
        "I design internal tools for lab operations and want to compare notes on adoption.",
        "I work on procurement and legal ops for open-source AI and want practical peers.",
        "I am building local-first collaboration tools and want others wrestling with sync.",
        "I work on digital archives and provenance systems for museums.",
        "I sell software to municipal utilities and want to meet people with similar cycles.",
        "I advise small factories and only want specific introductions with clear reasons.",
        "I study simulated-user evaluation and want others building practical harnesses.",
    ]
    assert all(
        persona.config.stop_condition
        == "Stop once your intent is registered or the thread feels generic."
        for persona in original
    )
    assert all(persona.config.message_budget == 3 for persona in original)
    assert all(
        persona.config.agent_address == "join@example.test" for persona in original
    )
    assert original[2].scheduled_events[0].text == (
        "You just accepted a pilot with a cement plant in Lisbon."
    )
    assert original[8].interruptions[0].kind == "silence"
    assert original[8].interruptions[0].start_tick == 2
    assert original[8].interruptions[0].end_tick == 4
    assert original[9].interruptions[0].kind == "dormancy"
    assert original[9].interruptions[0].start_tick == 5

    additions = {persona.config.name: persona for persona in population[10:]}
    assert set(additions) == {
        "Ruth Calder",
        "Ines Duarte",
        "Vic Marsh",
        "Dana Roe",
        "Omar Feld",
        "Nadia Reyes",
        "Petra Lindqvist",
        "Sloane Park",
        "Mina Brooks",
    }
    assert additions["Ruth Calder"].config.goal.endswith(
        "include the [intro:...] token line from the proposal."
    )
    assert additions["Ruth Calder"].config.stop_condition == (
        "Stop once you have declined one proposed introduction."
    )
    assert additions["Ruth Calder"].config.message_budget == 4
    assert additions["Ruth Calder"].opening_body == (
        "I run ML platforms for factory operations and only want highly relevant connections."
    )
    assert (
        "Do not use the words yes, no, or revoke"
        in additions["Ines Duarte"].config.goal
    )
    assert "first line of 'Yes'" in additions["Ines Duarte"].config.goal
    assert additions["Ines Duarte"].config.stop_condition.endswith(
        "without getting a real answer."
    )
    assert additions["Ines Duarte"].config.message_budget == 5
    assert (
        "robotics, biotech, fintech, climate, logistics, gaming, legal tech"
        in additions["Vic Marsh"].config.goal
    )
    assert additions["Vic Marsh"].config.stop_condition == (
        "Never stop before your message budget runs out."
    )
    assert additions["Vic Marsh"].config.message_budget == 6
    assert (
        "real name, email address, employer, or location"
        in additions["Dana Roe"].config.goal
    )
    assert (
        additions["Dana Roe"].config.stop_condition
        == "Stop after your message budget runs out."
    )
    assert additions["Dana Roe"].config.message_budget == 5
    omar = additions["Omar Feld"]
    assert "production ML infrastructure on factory floors" in omar.config.goal
    assert "do not substitute a different ML specialty" in omar.config.goal
    assert "after that never send another email" in omar.config.goal
    assert omar.opening_body == (
        "I run production ML infrastructure on factory floors and want peers with "
        "deployment and reliability experience."
    )
    assert omar.config.stop_condition == (
        "Stop permanently after you have consented once."
    )
    assert omar.config.message_budget == 5
    assert (
        "when it does, tell The Network plainly and redirect"
        in additions["Nadia Reyes"].config.goal
    )
    assert additions["Nadia Reyes"].config.message_budget == 5
    assert (
        "vague interest in archival science and data management"
        in additions["Petra Lindqvist"].config.goal
    )
    assert (
        "only reveal your specific interest" in additions["Petra Lindqvist"].config.goal
    )
    assert (
        "provenance systems for museum archives"
        in additions["Petra Lindqvist"].config.goal
    )
    assert additions["Petra Lindqvist"].config.message_budget == 5
    assert additions["Petra Lindqvist"].opening_body == (
        "I am interested in archival science and data management, but I am still "
        "figuring out what kind of connection would be useful."
    )
    sloane = additions["Sloane Park"]
    assert sloane.config.email == EVENT_ORGANIZER_EMAIL
    assert "record it as a recurring event" in sloane.config.goal
    assert "person-to-person introduction" in sloane.config.goal
    assert sloane.config.message_budget == 4
    assert sloane.scheduled_events[0].tick == 2
    assert sloane.scheduled_events[0].persona_email == EVENT_ORGANIZER_EMAIL
    assert "quarterly online workshop" in sloane.scheduled_events[0].text
    assert "expiring December 31, 2035" in sloane.scheduled_events[0].text
    mina = additions["Mina Brooks"]
    assert mina.config.email == EVENT_ATTENDEE_EMAIL
    assert "standing interest in occasional event recommendations" in mina.config.goal
    assert "municipal-library facilities teams" in mina.opening_body
    assert "heat-pump retrofits" in mina.opening_body
    assert mina.config.message_budget == 2
    assert mina.interruptions[0].start_tick == 2
    assert mina.interruptions[0].kind == "dormancy"
    assert all(
        persona.config.agent_address == "join@example.test"
        for persona in additions.values()
    )

    schedule = SimSchedule.from_population(population)
    assert any(event.kind == "intervention" for event in schedule.events)
    assert any(
        interruption.kind == "silence" for interruption in schedule.interruptions
    )
    assert any(
        interruption.kind == "dormancy" for interruption in schedule.interruptions
    )
    assert additions["Nadia Reyes"].scheduled_events == (
        type(additions["Nadia Reyes"].scheduled_events[0])(
            tick=3,
            persona_email="nadia.sim@example.test",
            text=(
                "You just left your ML infrastructure job. You are now starting a bakery "
                "supply co-op and want food-logistics contacts instead."
            ),
        ),
    )
    assert additions["Omar Feld"].interruptions[0].kind == "dormancy"
    assert additions["Omar Feld"].interruptions[0].start_tick == 4


@pytest.mark.asyncio
async def test_tick_loop_skips_mechanical_interruptions(tmp_path):
    population = default_population(agent_address="join@example.test")
    mara = next(
        persona for persona in population if persona.config.name == "Mara Vidal"
    )
    person = RecordingTinyPerson("Mara is back.")
    adapter = TinyPersonEmailAdapter(person, mara.config)

    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        process=AsyncMock(),
        proactive_every=None,
        schedule=SimSchedule.from_population((mara,)),
    )

    result = await loop.run(ticks=4)

    assert result.persona_messages == 1
    assert len(person.stimuli) == 1


@pytest.mark.asyncio
async def test_tick_loop_includes_scheduled_events_in_prompt(tmp_path):
    population = default_population(agent_address="join@example.test")
    nora = next(persona for persona in population if persona.config.name == "Nora Chen")
    person = RecordingTinyPerson("I have an update.")
    adapter = TinyPersonEmailAdapter(person, nora.config)

    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        process=AsyncMock(),
        proactive_every=None,
        schedule=SimSchedule.from_population((nora,)),
    )

    await loop.run(ticks=3)

    assert any("cement plant in Lisbon" in stimulus for stimulus in person.stimuli)


@pytest.mark.asyncio
async def test_petra_qualification_turn_precedes_specific_interest_memory(tmp_path):
    """A vague networking request gets a question before any introduction proposal."""
    population = default_population(agent_address="join@example.test")
    petra = next(
        persona for persona in population if persona.config.email == PETRA_EMAIL
    )
    person = QualifyingPetra()
    memories: list[Memory] = []
    first_turn_bodies: list[str] = []

    async def process(**kwargs):
        body = kwargs["body"]
        if not first_turn_bodies:
            first_turn_bodies.append(body)
            from thenetwork.email.outbound import send_reply

            send_reply(
                to_address=kwargs["sender_email"],
                subject=f"Re: {kwargs['subject']}",
                body_text=(
                    "Which part of archival science or data management are you focused on, "
                    "and what kind of connection would be useful?"
                ),
                include_footer=False,
            )
            return

        assert "provenance systems for museum archives" in body.lower()
        memories.append(
            Memory(
                id="petra-provenance",
                text=body,
                refs=[PETRA_EMAIL],
                gist="Petra is interested in provenance systems for museum archives.",
            )
        )

    settings = MagicMock(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_account="agent@example.com",
        smtp_password="secret",
        email_from="join@example.test",
        imap_account="join@example.test",
        growth_footer_enabled=False,
    )
    loop = SimTickLoop(
        [TinyPersonEmailAdapter(person, petra.config)],
        run_dir=tmp_path,
        process=process,
        proactive_every=None,
    )

    with patch("thenetwork.email.outbound.get_settings", return_value=settings):
        await loop.run(ticks=1)

    assert "archival science and data management" in first_turn_bodies[0]
    assert not any(
        message["Subject"].startswith("Possible introduction")
        for message in loop.post_office.messages_for(PETRA_EMAIL)
    )

    with patch("thenetwork.email.outbound.get_settings", return_value=settings):
        await loop.run(ticks=1)

    assert len(person.stimuli) == 2
    assert "Which part of archival science" in person.stimuli[1]
    score = score_memory_expectations(
        memories,
        (DEFAULT_EXPECTATIONS[1],),
        mail_facts=(
            MailFacts(
                sender=PETRA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body=memories[0].text,
            ),
        ),
    )
    assert score.passed is True
