from __future__ import annotations

from contextlib import contextmanager
from email.message import EmailMessage
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.introductions import DigestReplyResult, process_consent_reply
from thenetwork.settings import get_settings
from thenetwork.sim.run.loop import (
    SimTickLoop,
    override_rate_limits,
    run_proactive_scans,
)
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
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

    with (
        patch("thenetwork.email.outbound.get_settings", return_value=settings),
        patch("thenetwork.email.outbound.smtplib.SMTP") as real_smtp,
    ):
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
    assert (
        "Meet Sam, they share your interest in ML infrastructure." in person.stimuli[1]
    )


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
    request["Subject"] = (
        "Possible introduction [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"
    )
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


TOKEN_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TOKEN_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _consent_request(to_email: str, token: str, message_id: str) -> EmailMessage:
    request = EmailMessage()
    request["From"] = "join@example.test"
    request["To"] = to_email
    request["Subject"] = f"Possible introduction [intro:{token}]"
    request["Message-ID"] = message_id
    request.set_content("A possible match came up. Reply YES to opt in.")
    return request


def _visible_lines(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]


@pytest.mark.asyncio
async def test_bundled_consent_tokens_are_answered_one_thread_per_turn(tmp_path):
    """Priya/Samir defect: a persona bundling every pending token into one email.

    The loop must present one consent thread per turn, so the delivered reply
    carries only the answered thread's token and the other request is answered
    on the next turn with its own token.
    """
    bundled = f"Yes\n[intro:{TOKEN_A}]\n[intro:{TOKEN_B}]"
    adapter = _adapter("Priya", "priya@example.test", [bundled, bundled], budget=2)
    process = AsyncMock()
    loop = SimTickLoop(
        [adapter], run_dir=tmp_path, process=process, proactive_every=None
    )
    loop.post_office.deliver(
        _consent_request("priya@example.test", TOKEN_A, "<req-a@example.test>")
    )
    loop.post_office.deliver(
        _consent_request("priya@example.test", TOKEN_B, "<req-b@example.test>")
    )

    result = await loop.run(ticks=2)

    assert result.persona_messages == 2
    first, second = process.await_args_list
    assert first.kwargs["subject"] == f"Re: Possible introduction [intro:{TOKEN_A}]"
    assert _visible_lines(first.kwargs["body"])[:2] == ["Yes", f"[intro:{TOKEN_A}]"]
    assert TOKEN_B not in first.kwargs["body"]
    assert second.kwargs["subject"] == f"Re: Possible introduction [intro:{TOKEN_B}]"
    assert _visible_lines(second.kwargs["body"])[:2] == ["Yes", f"[intro:{TOKEN_B}]"]
    assert TOKEN_A not in second.kwargs["body"]


@pytest.mark.asyncio
async def test_wrong_thread_token_is_rebound_to_the_answered_thread(tmp_path):
    """Ruth/Omar defect: a decision reply pasting a token from another thread."""
    adapter = _adapter(
        "Ruth", "ruth@example.test", [f"No\n[intro:{TOKEN_B}]"], budget=1
    )
    process = AsyncMock()
    loop = SimTickLoop(
        [adapter], run_dir=tmp_path, process=process, proactive_every=None
    )
    loop.post_office.deliver(
        _consent_request("ruth@example.test", TOKEN_A, "<req-a@example.test>")
    )

    await loop.run(ticks=1)

    call = process.await_args
    assert call.kwargs["subject"] == f"Re: Possible introduction [intro:{TOKEN_A}]"
    assert _visible_lines(call.kwargs["body"])[:2] == ["No", f"[intro:{TOKEN_A}]"]
    assert TOKEN_B not in call.kwargs["body"]


@pytest.mark.asyncio
async def test_clarifying_question_keeps_thread_token_without_fabricated_decision(
    tmp_path,
):
    """Ines-style reply: a question, not a decision, must pass through unchanged."""
    question = (
        f"Before deciding, could you say what this person works on?\n[intro:{TOKEN_A}]"
    )
    adapter = _adapter("Ines", "ines@example.test", [question], budget=1)
    process = AsyncMock()
    loop = SimTickLoop(
        [adapter], run_dir=tmp_path, process=process, proactive_every=None
    )
    loop.post_office.deliver(
        _consent_request("ines@example.test", TOKEN_A, "<req-a@example.test>")
    )

    await loop.run(ticks=1)

    lines = _visible_lines(process.await_args.kwargs["body"])
    assert lines[0] == "Before deciding, could you say what this person works on?"
    assert lines[1] == f"[intro:{TOKEN_A}]"


@pytest.mark.asyncio
async def test_stray_token_is_stripped_when_no_consent_thread_is_pending(tmp_path):
    adapter = _adapter(
        "Omar", "omar@example.test", [f"Thanks.\n[intro:{TOKEN_B}]"], budget=1
    )
    process = AsyncMock()
    loop = SimTickLoop(
        [adapter], run_dir=tmp_path, process=process, proactive_every=None
    )
    plain = EmailMessage()
    plain["From"] = "join@example.test"
    plain["To"] = "omar@example.test"
    plain["Subject"] = "Re: Simulation tick 1"
    plain["Message-ID"] = "<plain@example.test>"
    plain.set_content("Noted, I will keep an eye out.")
    loop.post_office.deliver(plain)

    await loop.run(ticks=1)

    assert "[intro:" not in process.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_tick_loop_flushes_intro_digests_on_the_proactive_cadence(tmp_path):
    """The gap this task fixes: queued digest candidates never reached mail.

    `flush_pending_digests` needs a live DB in production; the sim only needs
    to prove the tick loop actually calls it on the same cadence as the
    identify-only proactive scans, and that its result is surfaced on the
    `TickResult`/`SimLoopResult`.
    """
    process = AsyncMock()
    loop = SimTickLoop(
        [_adapter("Priya", "priya@example.test", ["one", "two"], budget=2)],
        run_dir=tmp_path,
        process=process,
        proactive_every=1,
    )

    with (
        patch.object(proactive.scan_for_opportunities, "func", AsyncMock()),
        patch.object(proactive.scan_for_matches, "func", AsyncMock()),
        patch(
            "thenetwork.sim.run.loop.flush_pending_digests",
            return_value={"digests_sent": 2},
        ) as flush,
    ):
        result = await loop.run(ticks=2)

    assert flush.call_count == 2
    assert [tick.digest_emails for tick in result.ticks] == [2, 2]
    assert result.digest_emails == 4


@pytest.mark.asyncio
async def test_persona_digest_selection_round_trips_through_digest_processing(
    tmp_path,
):
    """A persona replying to a `[digest:...]` email answers that thread, not
    a bundled `[intro:...]` token, and the reply reaches `process_digest_reply`
    with the digest token intact so the selection is consumed server-side.
    """
    adapter = _adapter("Alice", "alice@example.test", ["A"], budget=1)
    loop = SimTickLoop(
        [adapter],
        run_dir=tmp_path,
        proactive_every=None,
    )
    digest = EmailMessage()
    digest["From"] = "join@example.test"
    digest["To"] = "alice@example.test"
    digest["Subject"] = (
        "Possible introductions [digest:cccccccc-cccc-cccc-cccc-cccccccccccc]"
    )
    digest["Message-ID"] = "<digest@example.test>"
    digest.set_content(
        "A few possible matches came up:\n\nA. some gist\n\nReply with a letter."
    )
    loop.post_office.deliver(digest)

    with (
        patch("thenetwork.worker.tasks.get_session", session_factory(WorkerSession())),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_digest_reply",
            return_value=DigestReplyResult(handled=True, outcome="selected"),
        ) as digest_handler,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            new_callable=AsyncMock,
        ) as run_agent,
    ):
        result = await loop.run(ticks=1)

    assert result.persona_messages == 1
    assert digest_handler.call_args.kwargs["subject"] == (
        "Re: Possible introductions [digest:cccccccc-cccc-cccc-cccc-cccccccccccc]"
    )
    assert digest_handler.call_args.kwargs["body"].splitlines()[:2] == [
        "A",
        "[digest:cccccccc-cccc-cccc-cccc-cccccccccccc]",
    ]
    run_agent.assert_not_awaited()
