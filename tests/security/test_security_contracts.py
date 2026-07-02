"""Security-contract unit tests (THE SEAL).

These tests prove that the structural security guarantees hold regardless of
what the LLM outputs. They do not require a live DB or LLM.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.tools import dispatch_email, register_person
from thenetwork.db.models import Person
from thenetwork.search.match import MemoryMatch


# ---------------------------------------------------------------------------
# Capability email tool: opaque IDs only, address never exposed to caller
# ---------------------------------------------------------------------------

class FakeCtx:
    def __init__(
        self,
        sender_email: str = "alice@example.com",
        sender_user_id: str | None = "user-alice",
        sender_authenticated: bool = False,
    ):
        mock_sess = MagicMock()
        mock_sess.__enter__ = MagicMock(return_value=mock_sess)
        mock_sess.__exit__ = MagicMock(return_value=False)
        self.deps = AgentDeps(
            sender_email=sender_email,
            sender_user_id=sender_user_id,
            sender_authenticated=sender_authenticated,
            session_factory=lambda: mock_sess,
        )
        self._mock_sess = mock_sess


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
    fake_person = MagicMock(spec=Person)
    fake_person.email = "bob@example.com"

    ctx = FakeCtx()
    ctx._mock_sess.get.return_value = fake_person

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        result = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["to_address"] == "bob@example.com"
    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_dispatch_unknown_id_returns_error():
    """Unknown recipient ID must fail gracefully, not raise, not guess an address."""
    ctx = FakeCtx()
    ctx._mock_sess.get.return_value = None

    result = await dispatch_email(ctx, recipient_user_id="nonexistent", subject="Hi", body_text="Hello")

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# register_person: self-registration only — no confused-deputy re-opening
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_person_rejects_unauthenticated_sender():
    """An unauthenticated From: header must never mint a new identity."""
    ctx = FakeCtx(sender_user_id=None, sender_authenticated=False)

    result = await register_person(ctx, email="alice@example.com", name="Alice")

    assert result["status"] == "error"
    assert result["reason"] == "sender_not_authenticated"


@pytest.mark.asyncio
async def test_register_person_rejects_already_registered_sender():
    """A sender who already has a person_id cannot re-register (or hijack another id)."""
    ctx = FakeCtx(sender_user_id="user-alice", sender_authenticated=True)

    result = await register_person(ctx, email="alice@example.com", name="Alice")

    assert result["status"] == "error"
    assert result["reason"] == "already_registered"
    assert result["person_id"] == "user-alice"


@pytest.mark.asyncio
async def test_register_person_rejects_email_mismatch():
    """The tool cannot be used to register a third party — email must match the sender."""
    ctx = FakeCtx(
        sender_email="alice@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    )

    result = await register_person(ctx, email="bob@example.com", name="Bob")

    assert result["status"] == "error"
    assert result["reason"] == "email_mismatch"


@pytest.mark.asyncio
async def test_register_person_idempotent_when_already_exists():
    """A race/case-difference that left a DB row must return it, not raise."""
    ctx = FakeCtx(
        sender_email="alice@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    )
    existing = MagicMock(spec=Person, id="existing-id")
    ctx._mock_sess.exec.return_value.first.return_value = existing

    result = await register_person(ctx, email="alice@example.com", name="Alice")

    assert result["status"] == "exists"
    assert result["person_id"] == "existing-id"
    ctx._mock_sess.add.assert_not_called()


@pytest.mark.asyncio
async def test_register_person_creates_for_authenticated_new_sender():
    """The golden path: authenticated, unknown sender registering their own address."""
    ctx = FakeCtx(
        sender_email="alice@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    )
    ctx._mock_sess.exec.return_value.first.return_value = None

    def fake_refresh(person):
        person.id = "new-person-id"

    ctx._mock_sess.refresh.side_effect = fake_refresh

    result = await register_person(ctx, email="alice@example.com", name="Alice")

    assert result["status"] == "created"
    assert result["person_id"] == "new-person-id"
    ctx._mock_sess.add.assert_called_once()
    added_person = ctx._mock_sess.add.call_args.args[0]
    assert isinstance(added_person, Person)
    assert added_person.email == "alice@example.com"
    assert added_person.name == "Alice"


@pytest.mark.asyncio
async def test_register_person_case_insensitive_email_match():
    """Sender's own address should match regardless of case."""
    ctx = FakeCtx(
        sender_email="Alice@Example.com",
        sender_user_id=None,
        sender_authenticated=True,
    )
    ctx._mock_sess.exec.return_value.first.return_value = None
    ctx._mock_sess.refresh.side_effect = lambda person: setattr(person, "id", "new-id")

    result = await register_person(ctx, email="alice@example.com", name="Alice")

    assert result["status"] == "created"


# ---------------------------------------------------------------------------
# SEAL: memory storage creates gist; search returns only gist + opaque id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remember_stores_with_gist():
    """remember() must invoke sanitize_memory to produce a gist for cross-user eligibility."""
    from thenetwork.agent.tools import remember

    ctx = FakeCtx()
    ctx._mock_sess.get.return_value = MagicMock(spec=Person, id="user-alice")

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536) as mock_embed, \
         patch("thenetwork.agent.tools.sanitize_memory") as mock_sanitize:
        mock_sanitize.return_value = "alice is an ml engineer"
        await remember(ctx, text="Alice Smith is an ML engineer at Acme Corp, alice@acme.com", refs=["user-alice"])

    mock_sanitize.assert_called_once()
    mock_embed.assert_called_once()


@pytest.mark.asyncio
async def test_search_returns_gist_not_raw_text():
    """search() results must not include raw memory text — only gist (PII-stripped)."""
    from thenetwork.agent.tools import search

    mock_match = MemoryMatch(
        memory_id="mem-1",
        person_id="opaque-person-id",
        gist="ml engineer at a startup",
        similarity=0.88,
    )

    ctx = FakeCtx()
    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.match_memories", return_value=[mock_match]):
        results = await search(ctx, query="who works in ml")

    assert results
    r = results[0]
    assert "gist" in r
    assert "text" not in r, "raw memory text must never appear in search results"
    assert "name" not in r
    assert "email" not in r
    assert r["gist"] == "ml engineer at a startup"


@pytest.mark.asyncio
async def test_search_result_keys_sealed():
    """search() result dicts may only contain person_id, gist, similarity — nothing else."""
    from thenetwork.agent.tools import search

    mock_match = MemoryMatch(
        memory_id="mem-2",
        person_id="opaque-id",
        gist="does ml stuff",
        similarity=0.85,
    )

    ctx = FakeCtx()
    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.match_memories", return_value=[mock_match]):
        results = await search(ctx, query="ml engineer")

    allowed_keys = {"person_id", "gist", "similarity"}
    for r in results:
        leaked = set(r.keys()) - allowed_keys
        assert not leaked, f"unexpected keys leaked into search result: {leaked}"


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
