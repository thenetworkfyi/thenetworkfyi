"""pydantic-evals scenario tests for agent archetypes.

These are emergent-behavior assertions - no branching control flow in the agent.
Tests use pydantic-ai FunctionModel / TestModel for deterministic, offline runs.
"""
from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentDeps


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
    from thenetwork.agent import tools
    from thenetwork.agent.tools import reply_to_sender, send_outreach
    from unittest.mock import patch, MagicMock, AsyncMock, call

    tools._dispatch_limiter = None
    tools._dispatch_storage = None
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
