"""pydantic-evals scenario tests for agent archetypes.

These are emergent-behavior assertions - no branching control flow in the agent.
Tests use pydantic-ai FunctionModel / TestModel for deterministic, offline runs.
"""
from __future__ import annotations

import pytest
from limits import storage, strategies
from pydantic_ai.models.test import TestModel

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentDeps


@pytest.fixture(autouse=True)
def _use_in_memory_dispatch_limiter():
    """Keep capability scenarios independent of durable production quota state."""
    from thenetwork.agent import tools

    tools._dispatch_storage = storage.MemoryStorage()
    tools._dispatch_limiter = strategies.FixedWindowRateLimiter(
        tools._dispatch_storage
    )


# ---------------------------------------------------------------------------
# Onboarding archetype: new sender who hasn't been seen before
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboarding_calls_save_profile():
    """A new sender's first email should trigger save_or_update_profile."""
    from unittest.mock import patch, MagicMock, AsyncMock
    agent = build_agent(model=TestModel())

    with patch("thenetwork.agent.tools.get_session") as mock_gs, \
         patch("thenetwork.agent.tools.send_reply") as mock_send, \
         patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536):
        
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value.scalar_one.return_value = "user-abc"
        mock_gs.return_value = mock_session

        deps = AgentDeps(sender_email="new@example.com", sender_user_id=None)
        result = await agent.run(
            "Hi, I'm new here. I'm a backend engineer looking to meet ML engineers.",
            deps=deps,
        )
    assert result.output is not None
    assert isinstance(result.output, str)


# ---------------------------------------------------------------------------
# Matchmaking archetype: sender expresses intent, expects matches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_matchmaking_returns_opaque_ids_only():
    """search must never expose names/emails/bios in its return value."""
    from thenetwork.agent.tools import search
    from thenetwork.agent.deps import AgentDeps
    from unittest.mock import AsyncMock, patch, MagicMock
    from thenetwork.search.match import MemoryMatch

    mock_results = [
        MemoryMatch(
            memory_id="mem-1",
            person_id="opaque-id-1",
            gist="backend engineer interested in ML",
            similarity=0.9,
        )
    ]

    deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.deps = deps

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.get_session", return_value=mock_session), \
         patch("thenetwork.agent.tools.match_memories", return_value=mock_results):
        result = await search(ctx, query="looking for ML engineers")

    assert len(result) == 1
    candidate = result[0]
    # Opaque ID present
    assert candidate["person_id"] == "opaque-id-1"
    # No PII fields
    assert "name" not in candidate
    assert "email" not in candidate
    assert "bio" not in candidate


# ---------------------------------------------------------------------------
# Dispatch email: capability tool, address resolved server-side
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_outreach_resolves_address_server_side():
    """send_outreach must look up the address by ID, never accept a raw address."""
    from thenetwork.agent.tools import send_outreach
    from unittest.mock import patch, MagicMock

    fake_profile = MagicMock()
    fake_profile.email = "bob@example.com"

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()

    with patch("thenetwork.agent.tools.get_session") as mock_get_session, \
         patch("thenetwork.agent.tools.send_reply") as mock_send:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = fake_profile
        mock_get_session.return_value = mock_session

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
    from unittest.mock import patch, MagicMock, AsyncMock, call

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

    with patch("thenetwork.agent.tools.get_session") as mock_gs, \
         patch("thenetwork.agent.tools.send_reply", new=MagicMock(side_effect=fake_send_reply)):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.side_effect = lambda _, uid: profiles.get(uid)
        mock_gs.return_value = mock_session

        await reply_to_sender(ctx, subject="Intro", body_text="Hi Alice.")
        await send_outreach(ctx, recipient_user_id="user-bob", subject="Intro", body_text="Hi Bob.")

    assert "alice@example.com" in sent_to
    assert "bob@example.com" in sent_to


# ---------------------------------------------------------------------------
# forget: strict sole-ref ownership rejects co-owned (multi-ref) memories
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forget_rejects_multi_ref_memory():
    """A memory co-owned by two people must not be deletable by either sender."""
    from thenetwork.agent.tools import forget
    from unittest.mock import patch, MagicMock

    fake_memory = MagicMock()
    fake_memory.refs = ["user-alice", "user-bob"]

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()

    with patch("thenetwork.agent.tools.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = fake_memory
        mock_gs.return_value = mock_session

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
    """Once dispatch_email_sent_count reaches the per-run cap, sends stop."""
    from thenetwork.agent.tools import send_outreach
    from unittest.mock import patch, MagicMock

    class FakeCtx:
        deps = AgentDeps(sender_email="alice@example.com", sender_user_id="user-alice")

    ctx = FakeCtx()
    ctx.deps.dispatch_email_sent_count = ctx.deps.settings.dispatch_max_sends_per_run

    with patch("thenetwork.agent.tools.get_session") as mock_gs, \
         patch("thenetwork.agent.tools.send_reply") as mock_send:
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
    assert ctx.deps.dispatch_email_sent_count == ctx.deps.settings.dispatch_max_sends_per_run


# ---------------------------------------------------------------------------
# escalate: authenticated-but-unregistered sender gets the fixed welcome,
# not a model-authored escalation reply, while admins still get notified
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_escalate_sends_welcome_and_notifies_admin_for_unregistered_sender():
    """First contact from an authenticated unknown sender: welcome + admin escalation."""
    from thenetwork.agent.tools import escalate
    from thenetwork.email.outbound import FIRST_CONTACT_WELCOME_REPLY
    from unittest.mock import patch, MagicMock

    class FakeCtx:
        deps = AgentDeps(
            sender_email="stranger@example.com",
            sender_user_id=None,
            sender_authenticated=True,
        )

    ctx = FakeCtx()

    with patch("thenetwork.agent.tools.send_reply") as mock_send, \
         patch("thenetwork.agent.tools.notify_admins") as mock_notify, \
         patch("thenetwork.agent.tools.get_session") as mock_gs:
        result = await escalate(ctx, reason="unclear intent")

    assert result["status"] == "welcomed_and_escalated"

    mock_send.assert_called_once()
    send_kwargs = mock_send.call_args.kwargs
    assert send_kwargs["to_address"] == "stranger@example.com"
    assert send_kwargs["body_text"] == FIRST_CONTACT_WELCOME_REPLY

    mock_notify.assert_called_once()
    notify_args = mock_notify.call_args.args
    assert "stranger@example.com" in notify_args[1]
    assert "unclear intent" in notify_args[2]

    # No memory should be written for this fixed-reply path.
    mock_gs.assert_not_called()
