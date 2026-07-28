"""pydantic-evals scenario tests for agent archetypes.

These are emergent-behavior assertions - no branching control flow in the agent.
Tests use pydantic-ai FunctionModel for deterministic, offline runs.
"""

from __future__ import annotations

import pytest
from limits import storage, strategies
from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentCapabilities, AgentDeps


@pytest.fixture(autouse=True)
def _use_in_memory_dispatch_limiter():
    """Keep capability scenarios independent of durable production quota state."""
    from thenetwork.agent import tools

    tools._dispatch_storage = storage.MemoryStorage()
    tools._dispatch_limiter = strategies.FixedWindowRateLimiter(tools._dispatch_storage)


async def _run_attachment_reply_scenario(attachment_count: int) -> dict[str, str]:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from pydantic_ai.messages import (
        ModelMessage,
        ModelResponse,
        TextPart,
        ToolCallPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from thenetwork.agent.core import run_agent_for_email

    model_calls = 0

    async def reply_model(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls > 1:
            return ModelResponse(parts=[TextPart(content="Reply sent.")])

        user_text = "\n".join(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        attachment_present = "Attachments present but not read:" in user_text
        body_text = (
            "The attachment was not read. Please paste the relevant content into "
            "the email so I can help with it."
            if attachment_present
            else "Thanks for the note. I have the details you included in the email."
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="reply_to_sender",
                    args={
                        "subject": "Re: Project details",
                        "body_text": body_text,
                        "sent_email_summary": (
                            "explained that an attachment was not read"
                            if attachment_present
                            else "acknowledged the project details"
                        ),
                    },
                )
            ]
        )

    agent = build_agent(model=FunctionModel(reply_model))
    settings = SimpleNamespace(
        agent_model="test:model",
        agent_request_limit=4,
        agent_total_tokens_limit=20_000,
        response_log_redaction_secret="",
    )
    sent: list[dict[str, str]] = []

    def capture_reply(*, to_address: str, subject: str, body_text: str, **_kwargs):
        sent.append({"to": to_address, "subject": subject, "body": body_text})

    capabilities = AgentCapabilities(
        send_reply=MagicMock(side_effect=capture_reply),
        record_sent_email_memory=AsyncMock(),
    )
    with (
        patch("thenetwork.agent.core.get_settings", return_value=settings),
        patch("thenetwork.agent.core.build_agent", return_value=agent),
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap"),
    ):
        await run_agent_for_email(
            sender_email="sender@example.com",
            sender_user_id=None,
            sender_authenticated=True,
            email_subject="Project details",
            email_body="Please review the material I sent.",
            attachment_count=attachment_count,
            capabilities=capabilities,
        )

    assert len(sent) == 1
    return sent[0]


@pytest.mark.asyncio
async def test_attachment_present_is_acknowledged_in_the_reply():
    reply = await _run_attachment_reply_scenario(attachment_count=1)

    assert reply["to"] == "sender@example.com"
    assert "attachment was not read" in reply["body"].lower()
    assert "paste" in reply["body"].lower()


@pytest.mark.asyncio
async def test_no_attachment_does_not_create_a_phantom_attachment_mention():
    reply = await _run_attachment_reply_scenario(attachment_count=0)

    assert reply["to"] == "sender@example.com"
    assert "attachment" not in reply["body"].lower()
    assert "file" not in reply["body"].lower()


# ---------------------------------------------------------------------------
# Onboarding archetype: new sender who hasn't been seen before
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onboarding_registers_sender_then_remembers_under_sender_id():
    """A new sender is registered before their first-contact note is stored."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from thenetwork.db.models import Person

    call_count = 0

    async def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="register_person",
                        args={"name": "Priya"},
                    )
                ]
            )
        if call_count == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="remember",
                        args={
                            "text": "backend engineer looking to meet ML engineers",
                            "refs": ["user-priya"],
                        },
                    )
                ]
            )
        if call_count == 3:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args={
                            "subject": "Re: Welcome",
                            "body_text": "I recorded what you are looking for.",
                            "sent_email_summary": "confirmed the sender's standing intent",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Onboarding recorded.")])

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.exec.return_value.first.return_value = None
    mock_session.exec.return_value.one.return_value = 0

    def refresh_person(person: Person) -> None:
        person.id = "user-priya"

    mock_session.refresh.side_effect = refresh_person

    def fake_sanitize(memory, session) -> str:
        return "backend engineer looking to meet ML engineers"

    send = MagicMock()
    capabilities = AgentCapabilities(
        default_session_factory=lambda: mock_session,
        embed_text=AsyncMock(return_value=[0.0] * 1536),
        match_memories=MagicMock(return_value=[]),
        sanitize_memory=MagicMock(side_effect=fake_sanitize),
        send_reply=send,
        record_sent_email_memory=AsyncMock(),
    )
    with (
        patch("thenetwork.agent.tools._hit_registration_quota", return_value=True),
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap"),
    ):
        deps = AgentDeps(
            capabilities=capabilities,
            sender_email="priya@example.com",
            sender_user_id=None,
            sender_authenticated=True,
        )
        result = await build_agent(model=FunctionModel(script)).run(
            "Hi, I'm new here. I'm a backend engineer looking to meet ML engineers.",
            deps=deps,
        )

    tool_calls = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    assert [call.tool_name for call in tool_calls] == [
        "register_person",
        "remember",
        "reply_to_sender",
    ]
    assert tool_calls[1].args_as_dict()["refs"] == ["user-priya"]
    assert deps.sender_user_id == "user-priya"
    send.assert_called_once()


# ---------------------------------------------------------------------------
# Matchmaking archetype: sender expresses intent, expects matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matchmaking_returns_opaque_ids_only():
    """search must never expose names/emails/bios in its return value."""
    from thenetwork.agent.tools import search
    from thenetwork.agent.deps import AgentCapabilities, AgentDeps
    from unittest.mock import AsyncMock, MagicMock
    from thenetwork.search.match import MemoryMatch

    mock_results = [
        MemoryMatch(
            memory_id="mem-1",
            person_id="opaque-id-1",
            gist="backend engineer interested in ML",
            similarity=0.9,
        )
    ]

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    capabilities = AgentCapabilities(
        default_session_factory=lambda: mock_session,
        embed_text=AsyncMock(return_value=[0.0] * 1536),
        match_memories=MagicMock(return_value=mock_results),
    )
    deps = AgentDeps(
        capabilities=capabilities,
        sender_email="alice@example.com",
        sender_user_id="user-alice",
    )

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.deps = deps

    result = await search(ctx, query="looking for ML engineers")

    assert len(result) == 1
    candidate = result[0]
    # Opaque ID present
    assert candidate["person_id"] == "opaque-id-1"
    assert candidate["evidence"] == [{"gist": "backend engineer interested in ML"}]
    # No PII fields
    assert "name" not in candidate
    assert "email" not in candidate
    assert "bio" not in candidate


# ---------------------------------------------------------------------------
# Outreach email: capability tool, address resolved server-side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_outreach_resolves_address_server_side():
    """send_outreach must look up the address by ID, never accept a raw address."""
    from thenetwork.agent.tools import send_outreach
    from unittest.mock import MagicMock

    fake_profile = MagicMock()
    fake_profile.email = "bob@example.com"

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = fake_profile
    mock_send = MagicMock()
    ctx.deps.capabilities.default_session_factory = lambda: mock_session
    ctx.deps.capabilities.send_reply = mock_send

    result = await send_outreach(
        ctx,
        recipient_user_id="user-bob",
        subject="Hello",
        body_text="Let's connect.",
    )

    assert result["status"] == "sent"
    mock_send.assert_called_once()
    # The first positional arg must be bob's real address (resolved server-side)
    call_kwargs = mock_send.call_args.kwargs
    assert call_kwargs["to_address"] == "bob@example.com"


# ---------------------------------------------------------------------------
# Double-introduction: both parties emailed, no cross-disclosure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_intro_emails_both_parties():
    """The separate reply and outreach capabilities can email both parties."""
    from thenetwork.agent.tools import reply_to_sender, send_outreach
    from unittest.mock import MagicMock

    sent_to: list[str] = []

    def fake_send_reply(to_address, subject, body_text, body_html=None, **kwargs):
        sent_to.append(to_address)

    class FakeProfileA:
        email = "alice@example.com"

    class FakeProfileB:
        email = "bob@example.com"

    profiles = {"user-alice": FakeProfileA(), "user-bob": FakeProfileB()}

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.side_effect = lambda _, uid: profiles.get(uid)
    ctx.deps.capabilities.default_session_factory = lambda: mock_session
    ctx.deps.capabilities.send_reply = MagicMock(side_effect=fake_send_reply)

    await reply_to_sender(ctx, subject="Intro", body_text="Hi Alice.")
    await send_outreach(
        ctx, recipient_user_id="user-bob", subject="Intro", body_text="Hi Bob."
    )

    assert "alice@example.com" in sent_to
    assert "bob@example.com" in sent_to


# ---------------------------------------------------------------------------
# forget: strict sole-ref ownership rejects co-owned (multi-ref) memories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_rejects_multi_ref_memory():
    """A memory co-owned by two people must not be deletable by either sender."""
    from thenetwork.agent.tools import forget
    from unittest.mock import MagicMock

    fake_memory = MagicMock()
    fake_memory.refs = ["user-alice", "user-bob"]

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = fake_memory
    ctx.deps.capabilities.default_session_factory = lambda: mock_session

    result = await forget(ctx, memory_id="mem-shared")

    assert result["status"] == "forbidden"
    assert result["reason"] == "not_sender_memory"
    mock_session.delete.assert_not_called()
    mock_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# send_outreach: exhausted per-run send cap short-circuits before any send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_outreach_limited_once_run_cap_exhausted():
    """Once outbound_send_count reaches the per-run cap, sends stop."""
    from thenetwork.agent.tools import send_outreach
    from unittest.mock import MagicMock

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()
    ctx.deps.outbound_send_count = ctx.deps.settings.dispatch_max_sends_per_run

    mock_gs = MagicMock()
    mock_send = MagicMock()
    ctx.deps.capabilities.default_session_factory = mock_gs
    ctx.deps.capabilities.send_reply = mock_send

    result = await send_outreach(
        ctx,
        recipient_user_id="user-bob",
        subject="Hello",
        body_text="Let's connect.",
    )

    assert result["status"] == "limited"
    assert result["reason"] == "max_sends_per_run"
    mock_send.assert_not_called()
    mock_gs.assert_not_called()
    # The cap check happens before any recipient lookup or send, so the
    # sent-count side effect must not have advanced past the cap.
    assert ctx.deps.outbound_send_count == ctx.deps.settings.dispatch_max_sends_per_run


# ---------------------------------------------------------------------------
# escalate: authenticated-but-unregistered sender gets the fixed welcome,
# not a model-authored escalation reply, while admins still get notified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_sends_welcome_and_notifies_admin_for_unregistered_sender():
    """First contact from an authenticated unknown sender: welcome + admin escalation."""
    from thenetwork.agent.tools import escalate
    from thenetwork.email.render import (
        FirstContactWelcomeEmailContext,
        FixedEmailTemplate,
    )
    from unittest.mock import MagicMock

    class FakeCtx:
        deps = AgentDeps(
            sender_email="stranger@example.com",
            sender_user_id=None,
            sender_authenticated=True,
        )

    ctx = FakeCtx()

    mock_send = MagicMock()
    mock_notify = MagicMock()
    mock_gs = MagicMock()
    ctx.deps.capabilities.send_reply = mock_send
    ctx.deps.capabilities.notify_admins = mock_notify
    ctx.deps.capabilities.default_session_factory = mock_gs

    result = await escalate(ctx, reason="unclear intent")

    assert result["status"] == "welcomed_and_escalated"

    mock_send.assert_called_once()
    send_kwargs = mock_send.call_args.kwargs
    assert send_kwargs["to_address"] == "stranger@example.com"
    assert send_kwargs["fixed_template"] is FixedEmailTemplate.FIRST_CONTACT_WELCOME
    assert send_kwargs["fixed_context"] == FirstContactWelcomeEmailContext()

    mock_notify.assert_called_once()
    notify_args = mock_notify.call_args.args
    assert "stranger@example.com" in notify_args[1]
    assert "unclear intent" in notify_args[2]

    # No memory should be written for this fixed-reply path.
    mock_gs.assert_not_called()


# ---------------------------------------------------------------------------
# Qualification turn: a broad domain plus stated uncertainty is unqualified
# even when search surfaces a specific adjacent person - ask, remember the
# asked-note, propose nothing.
# ---------------------------------------------------------------------------


def _tool_call_names(result) -> list[str]:
    """Tool names in call order, extracted from the full agent message history."""
    from pydantic_ai.messages import ToolCallPart

    return [
        part.tool_name
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]


@pytest.mark.asyncio
async def test_vague_intent_qualification_asks_question_and_no_proposal():
    """A broad, self-described-uncertain domain gets one narrowing question and
    an asked-note, not a proposal - even with an adjacent search match present."""
    from unittest.mock import MagicMock, AsyncMock
    from pydantic_ai.models.function import FunctionModel, AgentInfo
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, TextPart
    from thenetwork.search.match import MemoryMatch

    call_count = 0

    async def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="search",
                        args={"query": "archival science and data management"},
                    )
                ]
            )
        if call_count == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="remember",
                        args={
                            "text": (
                                "asked user-petra which specific connection would help beyond "
                                "archival science and data management"
                            ),
                            "refs": ["user-petra"],
                        },
                    )
                ]
            )
        if call_count == 3:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args={
                            "subject": "Re: Archival science and data management",
                            "body_text": (
                                "Could you say more about what kind of connection would help - "
                                "a museum archive project, a data-management tool, something else?"
                            ),
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[TextPart(content="Asked for clarification, no proposal made.")]
        )

    agent = build_agent(model=FunctionModel(script))

    adjacent_match = [
        MemoryMatch(
            memory_id="mem-elise-1",
            person_id="user-elise",
            gist="works on museum archive digitization",
            similarity=0.62,
        )
    ]

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.exec.return_value.one.return_value = 0

    sent = []

    def fake_send_reply(to_address, subject, body_text, body_html=None, **kwargs):
        sent.append({"to": to_address, "subject": subject, "body": body_text})

    def fake_sanitize(memory, session):
        return "asked about narrowing a broad interest"

    capabilities = AgentCapabilities(
        default_session_factory=lambda: mock_session,
        send_reply=MagicMock(side_effect=fake_send_reply),
        embed_text=AsyncMock(return_value=[0.0] * 1536),
        match_memories=MagicMock(return_value=adjacent_match),
        sanitize_memory=MagicMock(side_effect=fake_sanitize),
    )
    deps = AgentDeps(
        capabilities=capabilities,
        sender_email="petra@example.com",
        sender_user_id="user-petra",
        sender_authenticated=True,
    )
    result = await agent.run(
        "I'm interested in archival science and data management broadly, but "
        "honestly I'm not sure yet what specific connection would actually help me.",
        deps=deps,
    )

    tool_names = _tool_call_names(result)

    assert "reply_to_sender" in tool_names
    assert "remember" in tool_names
    assert "propose_introduction" not in tool_names
    assert len(sent) == 1
    assert "?" in sent[0]["body"]


# ---------------------------------------------------------------------------
# Answer turn: the asked-note is forgotten and the specific interest is
# captured before any match is considered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vague_intent_answer_forgets_asked_note_and_captures_interest():
    """Once Petra names the specific interest, forget the asked-note and
    remember the specific interest, in that order."""
    from unittest.mock import MagicMock, AsyncMock
    from pydantic_ai.models.function import FunctionModel, AgentInfo
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, TextPart

    call_count = 0

    async def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="search",
                        args={"query": "archival science and data management"},
                    )
                ]
            )
        if call_count == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="forget",
                        args={"memory_id": "mem-asked-petra"},
                    )
                ]
            )
        if call_count == 3:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="remember",
                        args={
                            "text": "interested in museum archive provenance research specifically",
                            "refs": ["user-petra"],
                        },
                    )
                ]
            )
        if call_count == 4:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args={
                            "subject": "Re: Archival science and data management",
                            "body_text": (
                                "Got it - provenance research is specific enough that I can "
                                "watch for the right person."
                            ),
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[TextPart(content="Captured the specific interest.")]
        )

    agent = build_agent(model=FunctionModel(script))

    asked_note = MagicMock()
    asked_note.refs = ["user-petra"]

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.exec.return_value.one.return_value = 0
    mock_session.get.return_value = asked_note

    sent = []

    def fake_send_reply(to_address, subject, body_text, body_html=None, **kwargs):
        sent.append({"to": to_address, "subject": subject, "body": body_text})

    def fake_sanitize(memory, session):
        return "interested in museum archive provenance research"

    capabilities = AgentCapabilities(
        default_session_factory=lambda: mock_session,
        send_reply=MagicMock(side_effect=fake_send_reply),
        embed_text=AsyncMock(return_value=[0.0] * 1536),
        match_memories=MagicMock(return_value=[]),
        sanitize_memory=MagicMock(side_effect=fake_sanitize),
    )
    deps = AgentDeps(
        capabilities=capabilities,
        sender_email="petra@example.com",
        sender_user_id="user-petra",
        sender_authenticated=True,
    )
    result = await agent.run(
        "Museum archive provenance research specifically - that's what I'm after.",
        deps=deps,
    )

    tool_names = _tool_call_names(result)

    assert "forget" in tool_names
    assert "remember" in tool_names
    assert tool_names.index("forget") < tool_names.index("remember")
    mock_session.delete.assert_called_once_with(asked_note)


# ---------------------------------------------------------------------------
# propose_introduction: a non-proposed result must carry an explicit note so
# a reply-writing model cannot mistake "tool ran" for "a request went out".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppressed_introduction_result_carries_no_send_note():
    """A suppressed propose_introduction result must tell the model plainly
    that no consent request was sent, independent of any reply it later writes."""
    from unittest.mock import MagicMock
    from pydantic_ai.models.function import FunctionModel, AgentInfo
    from pydantic_ai.messages import (
        ModelMessage,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        TextPart,
    )

    call_count = 0

    async def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="propose_introduction",
                        args={
                            "other_person_id": "user-dana",
                            "sender_gist": "backend engineer interested in ML",
                            "other_gist": "ML engineer interested in backend systems",
                        },
                    )
                ]
            )
        if call_count == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="reply_to_sender",
                        args={
                            "subject": "Re: looking for an ML engineer",
                            "body_text": (
                                "Nothing that qualified to send this time - I'll keep "
                                "this in mind."
                            ),
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="No proposal sent this run.")])

    agent = build_agent(model=FunctionModel(script))

    fake_profile = MagicMock()
    fake_profile.email = "jordan@example.com"

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = fake_profile

    sent = []

    def fake_send_reply(to_address, subject, body_text, body_html=None, **kwargs):
        sent.append({"to": to_address, "subject": subject, "body": body_text})

    capabilities = AgentCapabilities(
        default_session_factory=lambda: mock_session,
        send_reply=MagicMock(side_effect=fake_send_reply),
        propose_pair=MagicMock(
            return_value={"status": "suppressed", "reason": "declined"}
        ),
    )
    deps = AgentDeps(
        capabilities=capabilities,
        sender_email="jordan@example.com",
        sender_user_id="user-jordan",
        sender_authenticated=True,
    )
    result = await agent.run(
        "Any luck finding me an ML engineer to talk to?",
        deps=deps,
    )

    tool_names = _tool_call_names(result)
    assert "propose_introduction" in tool_names
    assert "reply_to_sender" in tool_names

    intro_returns = [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "propose_introduction"
    ]
    assert len(intro_returns) == 1
    assert intro_returns[0]["status"] == "suppressed"
    assert "no consent request was sent" in intro_returns[0]["note"]

    assert len(sent) == 1
    assert "reached out" not in sent[0]["body"].lower()
    assert "expect" not in sent[0]["body"].lower()
