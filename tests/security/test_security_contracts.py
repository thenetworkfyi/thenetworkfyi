"""Security-contract unit tests (THE SEAL).

These tests prove that the structural security guarantees hold regardless of
what the LLM outputs. They do not require a live DB or LLM.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.tools import (
    dispatch_email,
    inspect_user_profile,
    search_candidates,
)
from thenetwork.db.models import Profile
from thenetwork.search.match import MatchResult


# ---------------------------------------------------------------------------
# Capability email tool: opaque IDs only, address never exposed to caller
# ---------------------------------------------------------------------------

class FakeCtx:
    def __init__(self, sender_email="alice@example.com", sender_user_id="user-alice"):
        self.deps = AgentDeps(sender_email=sender_email, sender_user_id=sender_user_id)


@pytest.mark.asyncio
async def test_dispatch_resolves_address_not_from_caller():
    """The tool signature takes only recipient_user_id — caller cannot supply a raw address."""
    import inspect
    sig = inspect.signature(dispatch_email)
    params = list(sig.parameters.keys())
    assert "recipient_user_id" in params
    assert "to_address" not in params
    assert "email" not in params


@pytest.mark.asyncio
async def test_dispatch_sends_to_resolved_address():
    """Address must come from DB lookup, not from any agent-supplied argument."""
    fake_profile = MagicMock(spec=Profile)
    fake_profile.email = "bob@example.com"

    ctx = FakeCtx()
    with patch("thenetwork.agent.tools.get_session") as mock_gs, \
         patch("thenetwork.agent.tools.send_reply") as mock_send:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = fake_profile
        mock_gs.return_value = mock_session

        result = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["to_address"] == "bob@example.com"
    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_dispatch_unknown_id_returns_error():
    """Unknown recipient ID must fail gracefully, not raise, not guess an address."""
    ctx = FakeCtx()
    with patch("thenetwork.agent.tools.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = None
        mock_gs.return_value = mock_session

        result = await dispatch_email(ctx, recipient_user_id="nonexistent", subject="Hi", body_text="Hello")

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Minimal disclosure: other-user inspect returns no PII
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inspect_own_profile_returns_full_data():
    fake_profile = MagicMock(spec=Profile)
    fake_profile.id = "user-alice"
    fake_profile.name = "Alice"
    fake_profile.email = "alice@example.com"
    fake_profile.bio = "My bio"
    fake_profile.skills = ["python"]
    fake_profile.intent_description = "Looking for ML"
    fake_profile.available_to_collaborate = True

    ctx = FakeCtx(sender_user_id="user-alice")
    with patch("thenetwork.agent.tools.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = fake_profile
        mock_gs.return_value = mock_session

        result = await inspect_user_profile(ctx, user_id="user-alice")

    assert result["name"] == "Alice"
    assert "bio" in result


@pytest.mark.asyncio
async def test_inspect_other_user_returns_no_pii():
    fake_profile = MagicMock(spec=Profile)
    fake_profile.id = "user-bob"
    fake_profile.name = "Bob"
    fake_profile.email = "bob@example.com"
    fake_profile.bio = "Bob's secret bio"
    fake_profile.skills = ["rust"]
    fake_profile.intent_description = "Rust dev"
    fake_profile.available_to_collaborate = True

    ctx = FakeCtx(sender_user_id="user-alice")  # different user
    with patch("thenetwork.agent.tools.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = fake_profile
        mock_gs.return_value = mock_session

        result = await inspect_user_profile(ctx, user_id="user-bob")

    # Name and email must not appear
    assert "name" not in result
    assert "email" not in result
    assert "bio" not in result
    # Non-identifying fields are fine
    assert "skills" in result
    assert "id" in result


# ---------------------------------------------------------------------------
# Search candidates: opaque IDs, no PII in results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_candidates_no_pii_in_results():
    mock_match = MatchResult(
        user_id="user-bob",
        similarity=0.9,
        mutual_connections=0.3,
        combined_score=0.72,
        skill_overlap=["python"],
    )

    ctx = FakeCtx()
    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.match_candidates", new_callable=AsyncMock, return_value=[mock_match]):
        results = await search_candidates(ctx, intent_text="ML engineer")

    assert len(results) == 1
    r = results[0]
    assert r["user_id"] == "user-bob"
    assert "name" not in r
    assert "email" not in r
    assert "bio" not in r


# ---------------------------------------------------------------------------
# Mail-loop prevention: RFC 3834 header skipping
# ---------------------------------------------------------------------------

def test_auto_submitted_messages_are_skipped():
    """Messages with Auto-Submitted != no must be filtered out by the poller."""
    from thenetwork.email.inbound import _is_auto_message

    class FakeMsg:
        def __init__(self, headers):
            self.headers = headers

    assert _is_auto_message(FakeMsg({"auto-submitted": ["auto-generated"]})) is True
    assert _is_auto_message(FakeMsg({"auto-submitted": ["auto-replied"]})) is True
    assert _is_auto_message(FakeMsg({"auto-submitted": ["no"]})) is False
    assert _is_auto_message(FakeMsg({"precedence": ["bulk"]})) is True
    assert _is_auto_message(FakeMsg({"precedence": ["list"]})) is True
    assert _is_auto_message(FakeMsg({})) is False


def test_outbound_has_auto_submitted_header():
    """Outbound email must set Auto-Submitted: auto-generated to prevent re-ingestion."""
    from email.message import EmailMessage
    from unittest.mock import patch, MagicMock
    import smtplib

    captured: list[EmailMessage] = []

    def fake_send_message(msg):
        captured.append(msg)

    with patch("thenetwork.email.outbound.get_settings") as mock_settings, \
         patch("smtplib.SMTP") as mock_smtp:
        s = MagicMock()
        s.smtp_host = "smtp.example.com"
        s.smtp_port = 587
        s.email_account = "agent@example.com"
        s.email_password = "secret"
        mock_settings.return_value = s

        smtp_instance = MagicMock()
        smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
        smtp_instance.__exit__ = MagicMock(return_value=False)
        smtp_instance.send_message.side_effect = fake_send_message
        mock_smtp.return_value = smtp_instance

        from thenetwork.email.outbound import send_reply
        send_reply(to_address="bob@example.com", subject="Hi", body_text="Hello")

    assert len(captured) == 1
    assert captured[0]["Auto-Submitted"] == "auto-replied"


# ---------------------------------------------------------------------------
# Rate limiting: over-quota sender is blocked
# ---------------------------------------------------------------------------

def test_rate_limit_blocks_after_quota():
    """A sender who exceeds their hourly quota must be blocked."""
    from unittest.mock import patch, MagicMock
    from thenetwork.security.rate_limit import check_rate_limit

    call_count = 0

    def fake_hit(limit, key):
        nonlocal call_count
        call_count += 1
        return call_count <= 10  # allow first 10, block 11th

    mock_limiter = MagicMock()
    mock_limiter.hit.side_effect = fake_hit

    with patch("thenetwork.security.rate_limit._get_limiter", return_value=(mock_limiter, None)):
        results = [check_rate_limit("flood@attacker.com") for _ in range(12)]

    assert all(results[:10])   # first 10 allowed
    assert not results[10]     # 11th blocked
    assert not results[11]     # 12th blocked
