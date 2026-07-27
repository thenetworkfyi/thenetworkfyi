"""Deterministic real-process simulation for the double-opt-in introduction flow."""

from __future__ import annotations

import mailbox
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlmodel import select

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.tools import propose_introduction
from thenetwork.audit import audit_jsonl_file
from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.db.session import get_session
from thenetwork.sim.run.database import new_sim_database_name, provision_sim_database
from thenetwork.sim.run.loop import override_rate_limits
from thenetwork.sim.run.mail import (
    SimMessageMeta,
    SimPostOffice,
    capture_outbound,
    deliver_inbound,
    render_transcript,
)
from thenetwork.sim.personas.persona import PersonaConfig
from thenetwork.sim.run.recorder import (
    Clock,
    EventsLog,
    SimRunArtifacts,
    _record_delivered_message,
    create_run_artifacts,
    prepare_private_artifacts,
    write_redacted_json,
)
from thenetwork.sim.scoring.scoring import (
    PersonaPII,
    score_seal_mbox,
)
from thenetwork.settings import get_settings
from thenetwork.worker.tasks import process_email


ProgressCallable = Callable[[str], None]

INTRO_FLOW_PERSONAS = (
    PersonaConfig(
        name="Alice Rivera",
        email="alice.intro@example.test",
        goal="Find a collaborator building privacy-preserving developer tools.",
        stop_condition="A mutually consented introduction is made.",
    ),
    PersonaConfig(
        name="Bob Chen",
        email="bob.intro@example.test",
        goal="Find a collaborator building secure developer infrastructure.",
        stop_condition="A mutually consented introduction is made.",
    ),
)

_ALICE_GIST = "Builds privacy-preserving developer tools and seeks a collaborator."
_BOB_GIST = "Builds secure developer infrastructure and seeks a collaborator."
_RELAY_DOMAIN = "relay.thenetwork.test"


@dataclass(frozen=True)
class _SeededPerson:
    id: str
    email: str


async def run_intro_flow_sim(
    *,
    runs_dir: Path,
    keep_db: bool = False,
    progress: ProgressCallable | None = None,
    clock: Clock | None = None,
) -> SimRunArtifacts:
    """Run the full introduction lifecycle without an LLM or external mail service."""
    database_name = new_sim_database_name()
    artifacts = None
    with provision_sim_database(
        database_name,
        keep=keep_db,
        dump_path=lambda: (
            None if artifacts is None else artifacts.raw_database_dump_path
        ),
    ):
        artifacts = await _record_intro_flow(
            runs_dir=runs_dir,
            database_name=database_name,
            progress=progress,
            clock=clock,
        )
    return artifacts


async def _record_intro_flow(
    *,
    runs_dir: Path,
    database_name: str,
    progress: ProgressCallable | None,
    clock: Clock | None,
) -> SimRunArtifacts:
    artifacts = create_run_artifacts(runs_dir, clock=clock)
    artifacts.run_dir.mkdir(parents=True, exist_ok=False)
    prepare_private_artifacts(artifacts)
    events = EventsLog(artifacts.events_path)
    post_office = SimPostOffice(
        mbox_path=artifacts.raw_mbox_path,
        on_deliver=_record_delivered_message(events),
    )
    alice, bob = _seed_personas()

    write_redacted_json(
        artifacts.config_path,
        {
            "database_name": database_name,
            "personas": [
                {
                    "email": persona.email,
                    "goal": persona.goal,
                    "message_budget": persona.message_budget,
                    "name": persona.name,
                    "stop_condition": persona.stop_condition,
                }
                for persona in INTRO_FLOW_PERSONAS
            ],
            "process_mode": "real",
            "scenario": "intro-flow",
        },
    )
    events.write("sim.run_started", scenario="intro-flow")

    with (
        audit_jsonl_file(artifacts.audit_path),
        override_rate_limits(10_000),
        _override_relay_domain(),
        capture_outbound(post_office),
    ):
        _report(progress, "proposing anonymous match")
        initial_trace_id = str(uuid4())
        post_office.deliver(
            _persona_message(
                INTRO_FLOW_PERSONAS[0],
                subject="Looking for a collaborator",
                body=INTRO_FLOW_PERSONAS[0].goal,
            ),
            SimMessageMeta(
                tick=1,
                direction="persona->agent",
                persona=INTRO_FLOW_PERSONAS[0].name,
                trace_id=initial_trace_id,
            ),
        )
        proposal = await propose_introduction(
            SimpleNamespace(
                deps=AgentDeps(
                    sender_email=alice.email,
                    sender_user_id=alice.id,
                    sender_authenticated=True,
                    trace_id=initial_trace_id,
                )
            ),
            other_person_id=bob.id,
            sender_gist=_ALICE_GIST,
            other_gist=_BOB_GIST,
        )
        if proposal != {"status": "proposed"}:
            raise RuntimeError(f"introduction proposal failed: {proposal}")
        token_subject = _proposal_subject(post_office, alice.email)
        events.write("sim.introduction_state", status="proposed")

        for tick, persona in enumerate(INTRO_FLOW_PERSONAS, start=2):
            _report(progress, f"{persona.name} replying YES")
            await _deliver_real_process(
                post_office=post_office,
                events=events,
                persona=persona,
                subject=f"Re: {token_subject}",
                body="YES",
                tick=tick,
            )
            events.write("sim.introduction_state", status=_consent_status())

        alice_intro = _introduction_message(post_office, alice.email)
        bob_intro = _introduction_message(post_office, bob.email)
        proxy_address = str(alice_intro.get("Reply-To", ""))
        if not proxy_address or str(bob_intro.get("Reply-To", "")) != proxy_address:
            raise RuntimeError("introduction messages did not share one proxy address")

        relay_exchanges = (
            (
                4,
                INTRO_FLOW_PERSONAS[0],
                bob.email,
                "I would like to compare our approaches to secure developer tools.",
            ),
            (
                5,
                INTRO_FLOW_PERSONAS[1],
                alice.email,
                "Agreed. I can share notes on the infrastructure tradeoffs.",
            ),
        )
        for tick, persona, destination, relay_body in relay_exchanges:
            before = len(post_office.messages_for(destination))
            _report(progress, f"{persona.name} replying through the proxy")
            await _deliver_real_process(
                post_office=post_office,
                events=events,
                persona=persona,
                to_address=proxy_address,
                subject="Re: Your introduction",
                body=relay_body,
                tick=tick,
            )
            delivered = len(post_office.messages_for(destination)) == before + 1
            events.write(
                "sim.relay_delivery",
                delivered=delivered,
                direction=(
                    "first_to_second"
                    if persona is INTRO_FLOW_PERSONAS[0]
                    else "second_to_first"
                ),
            )
            if not delivered:
                raise RuntimeError("introduced pair relay delivery failed")

        _report(progress, f"{INTRO_FLOW_PERSONAS[0].name} replying REVOKE")
        await _deliver_real_process(
            post_office=post_office,
            events=events,
            persona=INTRO_FLOW_PERSONAS[0],
            subject=f"Re: {token_subject}",
            body="REVOKE",
            tick=6,
        )
        final_status = _consent_status()
        events.write("sim.introduction_state", status=final_status)
        if final_status != "revoked":
            raise RuntimeError(f"expected revoked introduction, got {final_status}")

        bob_messages_before = len(post_office.messages_for(bob.email))
        await _deliver_real_process(
            post_office=post_office,
            events=events,
            persona=INTRO_FLOW_PERSONAS[0],
            to_address=proxy_address,
            subject="Re: Your introduction",
            body="This message must not be delivered after revocation.",
            tick=7,
        )
        revoked_delivery_blocked = (
            len(post_office.messages_for(bob.email)) == bob_messages_before
        )
        events.write(
            "sim.relay_delivery",
            delivered=not revoked_delivery_blocked,
            direction="first_to_second_after_revoke",
        )
        if not revoked_delivery_blocked:
            raise RuntimeError("revoked pair relayed a new message")

        outbound_before = _mbox_message_count(artifacts.raw_mbox_path)
        reproposal = await propose_introduction(
            SimpleNamespace(
                deps=AgentDeps(
                    sender_email=alice.email,
                    sender_user_id=alice.id,
                    sender_authenticated=True,
                    trace_id=str(uuid4()),
                )
            ),
            other_person_id=bob.id,
            sender_gist=_ALICE_GIST,
            other_gist=_BOB_GIST,
        )
        outbound_after = _mbox_message_count(artifacts.raw_mbox_path)
        blocked = (
            reproposal.get("status") == "suppressed"
            and reproposal.get("reason") == "revoked"
            and outbound_after == outbound_before
        )
        events.write(
            "sim.introduction_reproposal",
            blocked=blocked,
            reason=reproposal.get("reason"),
            status=reproposal.get("status"),
        )
        if not blocked:
            raise RuntimeError(f"revoked pair was facilitated again: {reproposal}")

        tier1 = score_seal_mbox(
            artifacts.raw_mbox_path,
            (PersonaPII.from_config(persona) for persona in INTRO_FLOW_PERSONAS),
        )
        events.write(
            "sim.score.tier1",
            passed=tier1.passed,
            findings=[
                {
                    "evidence": finding.evidence,
                    "message": finding.message,
                    "passed": finding.passed,
                    "tier": finding.tier,
                }
                for finding in tier1.findings
            ],
        )
        if not tier1.passed:
            raise RuntimeError("tier1 SEAL scoring failed")

    from thenetwork.sim.run.mail import publish_redacted_mbox

    publish_redacted_mbox(artifacts.raw_mbox_path, artifacts.mbox_path)
    render_transcript(artifacts.mbox_path, artifacts.transcript_path)
    events.write(
        "sim.run_completed",
        final_consent_state="revoked",
        relay_bidirectional=True,
        revoked_relay_blocked=True,
        reproposal_blocked=True,
        tier1_passed=True,
    )
    return artifacts


def _seed_personas() -> tuple[_SeededPerson, _SeededPerson]:
    people = tuple(
        Person(email=persona.email, name=persona.name)
        for persona in INTRO_FLOW_PERSONAS
    )
    with get_session() as session:
        session.add_all(people)
        session.commit()
        for person in people:
            session.refresh(person)
        return (
            _SeededPerson(id=people[0].id, email=people[0].email),
            _SeededPerson(id=people[1].id, email=people[1].email),
        )


async def _deliver_real_process(
    *,
    post_office: SimPostOffice,
    events: EventsLog,
    persona: PersonaConfig,
    to_address: str | None = None,
    subject: str,
    body: str,
    tick: int,
) -> None:
    trace_id = str(uuid4())
    events.write(
        "sim.process_email_started",
        persona=persona.name,
        subject=subject,
        trace_id=trace_id,
    )
    await deliver_inbound(
        _persona_message(
            persona,
            to_address=to_address,
            subject=subject,
            body=body,
        ),
        process=process_email.func,
        trace_id=trace_id,
        post_office=post_office,
        tick=tick,
        persona=persona.name,
    )
    events.write(
        "sim.process_email_completed",
        persona=persona.name,
        trace_id=trace_id,
    )


def _persona_message(
    persona: PersonaConfig,
    *,
    to_address: str | None = None,
    subject: str,
    body: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"{persona.name} <{persona.email}>"
    message["To"] = to_address or persona.agent_address
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    return message


def _proposal_subject(post_office: SimPostOffice, email: str) -> str:
    for message in post_office.messages_for(email):
        subject = str(message.get("Subject", ""))
        if subject.startswith("Possible introduction [intro:"):
            return subject
    raise RuntimeError(f"no consent request delivered to {email}")


def _introduction_message(post_office: SimPostOffice, email: str) -> EmailMessage:
    matches = [
        message
        for message in post_office.messages_for(email)
        if str(message.get("Subject", "")) == "Your introduction"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one introduction message for {email}")
    return matches[0]


@contextmanager
def _override_relay_domain():
    settings = get_settings()
    previous = settings.relay_domain
    settings.relay_domain = _RELAY_DOMAIN
    try:
        yield
    finally:
        settings.relay_domain = previous


def _consent_status() -> str:
    with get_session() as session:
        record = session.exec(select(IntroductionConsent)).one()
        return record.status


def _mbox_message_count(path: Path) -> int:
    box = mailbox.mbox(path)
    try:
        return len(box)
    finally:
        box.close()


def _report(progress: ProgressCallable | None, message: str) -> None:
    if progress is not None:
        progress(message)
