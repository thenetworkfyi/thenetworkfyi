from __future__ import annotations

from contextlib import contextmanager
from email.message import EmailMessage
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.introductions import process_consent_reply
from thenetwork.settings import get_settings
from thenetwork.sim.loop import SimTickLoop, override_rate_limits, run_proactive_scans
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.worker import proactive


class ScriptedTinyPerson:
    def __init__(self, replies: list[str]) -> None:
        self.name = "Scripted"
        self.replies = replies

    def listen_and_act(self, _stimulus: str):
        return {"content": self.replies.pop(0)}


class RecordingTinyPerson:
    """Records every stimulus it is shown, so tests can assert on prompt content."""

    def __init__(self, replies: list[str]) -> None:
        self.name = "Recording"
        self.replies = replies
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        return {"content": self.replies.pop(0)}


class Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class WorkerSession:
    def get(self, _model, _identity):
        return None

    def exec(self, _query):
        return Result("alice")


class ConsentSession:
    def __init__(self):
        self.proposal = IntroductionConsent(
            person_a_id="alice",
            person_b_id="bob",
            reply_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
        self.people = {
            "alice": Person(id="alice", name="Alice", email="alice@example.test"),
            "bob": Person(id="bob", name="Bob", email="bob@example.test"),
        }

    def exec(self, _query):
        return Result(self.proposal)

    def get(self, _model, person_id):
        return self.people.get(person_id)

    def add(self, value):
        self.proposal = value

    def commit(self):
        return None

    def refresh(self, _value):
        return None


def session_factory(session):
    @contextmanager
    def open_session():
        yield session

    return open_session


def _adapter(name: str, email: str, replies: list[str], budget: int = 2):
    return TinyPersonEmailAdapter(
        ScriptedTinyPerson(replies),
        PersonaConfig(
            name=name,
            email=email,
            goal="Find a strong match.",
            stop_condition="Stop when registered.",
            message_budget=budget,
            agent_address="join@example.test",
        ),
    )


@pytest.mark.asyncio
async def test_tick_loop_advances_time_and_processes_persona_messages(tmp_path):
    process = AsyncMock()
    loop = SimTickLoop(
        [_adapter("Priya", "priya@example.test", ["one", "two"], budget=2)],
        run_dir=tmp_path,
        process=process,
        proactive_every=10,
    )

    result = await loop.run(ticks=3)

    assert [tick.tick for tick in result.ticks] == [1, 2, 3]
    assert result.persona_messages == 2
    assert process.await_count == 2
    assert len(result.post_office.messages_for("join@example.test")) == 2


@pytest.mark.asyncio
async def test_proactive_scan_defers_are_executed_in_loop():
    async def fake_scan(_timestamp: int) -> None:
        proactive.process_email.defer(
            sender_email="priya@example.test",
            subject="[Proactive] Possible connection",
            body="opaque ids and gists only",
        )

    process = AsyncMock()

    count = await run_proactive_scans(timestamp=3, process=process, scans=(fake_scan,))

    assert count == 1
    process.assert_awaited_once_with(
        sender_email="priya@example.test",
        subject="[Proactive] Possible connection",
        body="opaque ids and gists only",
    )


def test_override_rate_limits_restores_settings():
    settings = get_settings()
    old = (
        settings.rate_limit_per_hour,
        settings.unauthenticated_rate_limit_per_hour,
        settings.global_email_rate_limit_per_hour,
    )

    with override_rate_limits(1234):
        assert settings.rate_limit_per_hour == 1234
        assert settings.unauthenticated_rate_limit_per_hour == 1234
        assert settings.global_email_rate_limit_per_hour >= 1234

    assert (
        settings.rate_limit_per_hour,
        settings.unauthenticated_rate_limit_per_hour,
        settings.global_email_rate_limit_per_hour,
    ) == old


@pytest.mark.asyncio
async def test_tick_loop_captures_replies_without_touching_real_smtp(tmp_path):
    settings = MagicMock()
    settings.smtp_host = "smtp.example.com"
    settings.smtp_port = 587
    settings.smtp_account = "agent@example.com"
    settings.smtp_password = "secret"
    settings.email_from = "agent@example.com"
    settings.imap_account = "join@example.com"
    settings.growth_footer_enabled = False

    async def process(**kwargs):
        from thenetwork.email.outbound import send_reply

        send_reply(
            to_address=kwargs["sender_email"],
            subject=f"Re: {kwargs['subject']}",
            body_text="Reply from the agent.",
        )

    loop = SimTickLoop(
        [_adapter("Priya", "priya@example.test", ["one"], budget=1)],
        run_dir=tmp_path,
        process=process,
        proactive_every=None,
    )

    with patch("thenetwork.email.outbound.get_settings", return_value=settings), patch(
        "thenetwork.email.outbound.smtplib.SMTP"
    ) as real_smtp:
        result = await loop.run(ticks=1)

    real_smtp.assert_not_called()
    (captured,) = result.post_office.messages_for("priya@example.test")
    assert captured["From"] == "agent@example.com"
    assert captured.get_content().strip() == "Reply from the agent."


@pytest.mark.asyncio
async def test_persona_turn_drains_post_office_reply_into_next_stimulus(tmp_path):
    person = RecordingTinyPerson(["initial ask", "thanks for the intro"])
    adapter = TinyPersonEmailAdapter(
        person,
        PersonaConfig(
            name="Priya",
            email="priya@example.test",
            goal="Find a strong match.",
            stop_condition="Stop when registered.",
            message_budget=2,
            agent_address="join@example.test",
        ),
    )

    async def scripted_process(**kwargs):
        reply = EmailMessage()
        reply["From"] = "join@example.test"
        reply["To"] = kwargs["sender_email"]
        reply["Subject"] = f"Re: {kwargs['subject']}"
        reply.set_content("Meet Sam, they share your interest in ML infrastructure.")
        loop.post_office.deliver(reply)

    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        process=scripted_process,
        proactive_every=None,
    )

    result = await loop.run(ticks=2)

    assert result.persona_messages == 2
    assert len(person.stimuli) == 2
    assert "Meet Sam" not in person.stimuli[0]
    assert "Meet Sam, they share your interest in ML infrastructure." in person.stimuli[1]


@pytest.mark.asyncio
async def test_tokened_persona_reply_round_trips_through_consent_processing(tmp_path):
    adapter = _adapter("Alice", "alice@example.test", ["YES"], budget=1)
    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        proactive_every=None,
    )
    request = EmailMessage()
    request["From"] = "join@example.test"
    request["To"] = "alice@example.test"
    request["Subject"] = "Possible introduction [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"
    request["Message-ID"] = "<proposal@example.test>"
    request.set_content("A possible match came up. Reply YES to opt in.")
    loop.post_office.deliver(request)

    consent_session = ConsentSession()
    with (
        patch("thenetwork.worker.tasks.get_session", session_factory(WorkerSession())),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            side_effect=partial(
                process_consent_reply,
                session_factory=session_factory(consent_session),
            ),
        ) as consent_handler,
        patch("thenetwork.introductions.send_reply"),
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            new_callable=AsyncMock,
        ) as run_agent,
    ):
        result = await loop.run(ticks=1)

    assert result.persona_messages == 1
    assert consent_session.proposal.person_a_consented is True
    assert consent_handler.call_args.kwargs["subject"] == (
        "Re: Possible introduction [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"
    )
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_loop_can_disable_proactive_scans(tmp_path):
    process = AsyncMock()
    loop = SimTickLoop(
        [_adapter("Priya", "priya@example.test", ["one"], budget=1)],
        run_dir=tmp_path,
        process=process,
        proactive_every=None,
    )

    result = await loop.run(ticks=1)

    assert result.proactive_jobs == 0
