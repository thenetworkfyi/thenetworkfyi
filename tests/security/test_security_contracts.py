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
from thenetwork.settings import Settings


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
            settings=Settings(),
            sender_email=sender_email,
            sender_user_id=sender_user_id,
            sender_authenticated=sender_authenticated,
            session_factory=lambda: mock_sess,
        )
        self._mock_sess = mock_sess


def _reset_dispatch_limiter():
    from thenetwork.agent import tools

    tools._dispatch_limiter = None
    tools._dispatch_storage = None


def _fake_person(email: str = "bob@example.com"):
    fake_person = MagicMock(spec=Person)
    fake_person.email = email
    return fake_person


@pytest.mark.asyncio
async def test_dispatch_resolves_address_not_from_caller():
    """The tool signature takes only recipient_user_id - caller cannot supply a raw address."""
    import inspect
    sig = inspect.signature(dispatch_email)
    params = list(sig.parameters.keys())
    assert "recipient_user_id" in params
    assert "to_address" not in params
    assert "email" not in params


@pytest.mark.asyncio
async def test_dispatch_sends_to_resolved_address():
    """Address must come from DB lookup, not from any agent-supplied argument."""
    _reset_dispatch_limiter()
    fake_person = _fake_person()

    ctx = FakeCtx()
    ctx._mock_sess.get.return_value = fake_person

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        result = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["to_address"] == "bob@example.com"
    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_dispatch_threads_reply_to_inbound_sender_only():
    _reset_dispatch_limiter()
    fake_person = _fake_person("alice@example.com")

    ctx = FakeCtx(sender_user_id="user-alice")
    ctx.deps.inbound_message_id = "<abc123@example.com>"
    ctx.deps.inbound_body_for_quote = "Original request"
    ctx.deps.inbound_date = "Sat, 04 Jul 2026 12:00:00 -0700"
    ctx._mock_sess.get.return_value = fake_person

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        result = await dispatch_email(
            ctx,
            recipient_user_id="user-alice",
            subject="Re: Hi",
            body_text="Hello",
        )

    assert result["status"] == "sent"
    assert mock_send.call_args.kwargs["in_reply_to"] == "<abc123@example.com>"
    assert mock_send.call_args.kwargs["references"] == "<abc123@example.com>"
    assert mock_send.call_args.kwargs["quoted_body_text"] == "Original request"
    assert mock_send.call_args.kwargs["quoted_date"] == "Sat, 04 Jul 2026 12:00:00 -0700"


@pytest.mark.asyncio
async def test_dispatch_does_not_thread_agent_outreach():
    _reset_dispatch_limiter()
    fake_person = _fake_person("bob@example.com")

    ctx = FakeCtx(sender_user_id="user-alice")
    ctx.deps.inbound_message_id = "<abc123@example.com>"
    ctx._mock_sess.get.return_value = fake_person

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        result = await dispatch_email(
            ctx,
            recipient_user_id="user-bob",
            subject="Intro",
            body_text="Hello",
        )

    assert result["status"] == "sent"
    assert "in_reply_to" not in mock_send.call_args.kwargs
    assert "references" not in mock_send.call_args.kwargs
    assert "quoted_body_text" not in mock_send.call_args.kwargs


@pytest.mark.asyncio
async def test_dispatch_never_quotes_inbound_text_to_third_party():
    _reset_dispatch_limiter()
    fake_person = _fake_person("bob@example.com")

    secret_inbound = "Alice private health note and alice.private@example.com"
    ctx = FakeCtx(sender_user_id="user-alice")
    ctx.deps.inbound_message_id = "<abc123@example.com>"
    ctx.deps.inbound_body_for_quote = secret_inbound
    ctx.deps.inbound_date = "Sat, 04 Jul 2026 12:00:00 -0700"
    ctx._mock_sess.get.return_value = fake_person

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        result = await dispatch_email(
            ctx,
            recipient_user_id="user-bob",
            subject="Intro",
            body_text="Hello",
        )

    assert result["status"] == "sent"
    assert "quoted_body_text" not in mock_send.call_args.kwargs
    assert secret_inbound not in repr(mock_send.call_args)


@pytest.mark.asyncio
async def test_proactive_graph_trigger_never_sets_quote_inputs():
    import networkx as nx

    from thenetwork.worker.proactive import scan_for_opportunities

    graph = nx.Graph()
    graph.add_edge("alice", "shared")
    graph.add_edge("bob", "shared")
    people = [MagicMock(id="alice", email="alice@example.com"), MagicMock(id="bob", email="bob@example.com")]
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.exec.return_value.all.return_value = people

    with patch("thenetwork.worker.proactive.build_graph", return_value=graph), \
         patch("thenetwork.worker.proactive.get_session", return_value=session), \
         patch("thenetwork.worker.proactive.process_email") as process_email:
        await scan_for_opportunities.func(0)

    assert process_email.defer.called
    for call in process_email.defer.call_args_list:
        assert "inbound_message_id" not in call.kwargs
        assert "inbound_body_for_quote" not in call.kwargs
        assert "inbound_date" not in call.kwargs


@pytest.mark.asyncio
async def test_proactive_semantic_trigger_never_sets_quote_inputs():
    import networkx as nx

    from thenetwork.search.match import MemoryMatch
    from thenetwork.worker.proactive import scan_for_matches

    recent = MagicMock(id="recent", refs=["arrival"], gist="arrival gist", embedding=[0.0])
    standing_person = MagicMock(id="standing", email="standing@example.com")
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.exec.return_value.all.return_value = [recent]
    session.get.return_value = standing_person
    matches = [MemoryMatch("older", "standing", "standing gist", 0.9)]

    with patch("thenetwork.worker.proactive.build_graph", return_value=nx.Graph()), \
         patch("thenetwork.worker.proactive.get_session", return_value=session), \
         patch("thenetwork.worker.proactive.match_memories", return_value=matches), \
         patch("thenetwork.worker.proactive.process_email") as process_email:
        await scan_for_matches.func(0)

    process_email.defer.assert_called_once()
    kwargs = process_email.defer.call_args.kwargs
    assert "inbound_message_id" not in kwargs
    assert "inbound_body_for_quote" not in kwargs
    assert "inbound_date" not in kwargs


def test_dispatch_cap_settings_defaults():
    assert Settings.model_fields["dispatch_max_sends_per_run"].default == 3
    assert Settings.model_fields["dispatch_recipient_daily_cap"].default == 3
    assert Settings.model_fields["dispatch_sender_reply_daily_cap"].default == 1


@pytest.mark.asyncio
async def test_dispatch_blocks_after_max_sends_per_run():
    _reset_dispatch_limiter()
    ctx = FakeCtx()
    ctx.deps.settings.dispatch_recipient_daily_cap = 99
    ctx.deps.settings.dispatch_sender_reply_daily_cap = 99
    ctx._mock_sess.get.return_value = _fake_person()

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        first = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")
        second = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")
        third = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")
        fourth = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")

    assert [first["status"], second["status"], third["status"]] == ["sent", "sent", "sent"]
    assert fourth == {"status": "limited", "reason": "max_sends_per_run", "limit": 3}
    assert mock_send.call_count == 3


@pytest.mark.asyncio
async def test_dispatch_recipient_daily_cap_is_settings_configurable():
    _reset_dispatch_limiter()
    ctx = FakeCtx()
    ctx.deps.settings.dispatch_max_sends_per_run = 99
    ctx.deps.settings.dispatch_recipient_daily_cap = 1
    ctx.deps.settings.dispatch_sender_reply_daily_cap = 99
    ctx._mock_sess.get.return_value = _fake_person()

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        first = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")
        second = await dispatch_email(ctx, recipient_user_id="user-bob", subject="Hi", body_text="Hello")

    assert first["status"] == "sent"
    assert second == {"status": "limited", "reason": "recipient_daily_cap", "limit": 1}
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_sender_reply_daily_cap_is_settings_configurable():
    _reset_dispatch_limiter()
    ctx = FakeCtx(sender_email="alice@example.com", sender_user_id="user-alice")
    ctx.deps.settings.dispatch_max_sends_per_run = 99
    ctx.deps.settings.dispatch_recipient_daily_cap = 99
    ctx.deps.settings.dispatch_sender_reply_daily_cap = 1
    ctx._mock_sess.get.return_value = _fake_person("alice@example.com")

    with patch("thenetwork.agent.tools.send_reply") as mock_send:
        first = await dispatch_email(ctx, recipient_user_id="user-alice", subject="Hi", body_text="Hello")
        second = await dispatch_email(ctx, recipient_user_id="user-alice", subject="Hi", body_text="Hello")

    assert first["status"] == "sent"
    assert second == {"status": "limited", "reason": "sender_reply_daily_cap", "limit": 1}
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_id_returns_error():
    """Unknown recipient ID must fail gracefully, not raise, not guess an address."""
    ctx = FakeCtx()
    ctx._mock_sess.get.return_value = None

    result = await dispatch_email(ctx, recipient_user_id="nonexistent", subject="Hi", body_text="Hello")

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# register_person: self-registration only - no confused-deputy re-opening
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
    """The tool cannot be used to register a third party - email must match the sender."""
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


@pytest.mark.asyncio
async def test_register_person_enforces_global_daily_quota():
    """New registrations stop when the configured global daily quota is exhausted."""
    ctx = FakeCtx(
        sender_email="alice@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    )
    ctx.deps.settings.registration_limit_per_day = 1
    ctx._mock_sess.exec.return_value.first.return_value = None

    limiter = MagicMock()
    limiter.hit.return_value = False

    with patch("thenetwork.agent.tools._get_registration_limiter", return_value=(limiter, None)):
        result = await register_person(ctx, email="alice@example.com", name="Alice")

    assert result == {
        "status": "error",
        "reason": "registration_quota_exceeded",
        "limit": 1,
    }
    ctx._mock_sess.add.assert_not_called()


# ---------------------------------------------------------------------------
# SEAL: memory storage creates gist; search returns only gist + opaque id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remember_stores_with_gist():
    """remember() must await high-fidelity sanitization for cross-user eligibility."""
    from thenetwork.agent.tools import remember

    ctx = FakeCtx()
    ctx._mock_sess.get.return_value = MagicMock(spec=Person, id="user-alice")
    sanitized = "[name] is an ml engineer"

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536) as mock_embed, \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize:
        mock_sanitize.return_value = sanitized
        await remember(ctx, text="Alice Smith is an ML engineer at Acme Corp, alice@acme.com", refs=["user-alice"])

    mock_sanitize.assert_awaited_once()
    mock_embed.assert_awaited_once_with(sanitized)


@pytest.mark.asyncio
async def test_remember_zero_ref_does_not_sanitize_or_set_gist():
    """Zero-ref memories remain raw general notes and do not get cross-user gists."""
    from thenetwork.agent.tools import remember

    ctx = FakeCtx()
    added: list[object] = []
    ctx._mock_sess.add.side_effect = added.append
    raw = "General system note with no person refs"

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536) as mock_embed, \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize:
        await remember(ctx, text=raw, refs=[])

    mock_sanitize.assert_not_awaited()
    mock_embed.assert_awaited_once_with(raw)
    assert added[0].gist is None


@pytest.mark.asyncio
async def test_remember_rejects_text_over_configured_cap():
    """Oversized memory text must fail before sanitize, embed, or insert."""
    from thenetwork.agent.tools import remember

    ctx = FakeCtx()
    ctx.deps.settings.remember_text_max_chars = 5

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock) as mock_embed, \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize:
        result = await remember(ctx, text="too long", refs=["user-alice"])

    assert result == {
        "status": "error",
        "reason": "memory_text_too_long",
        "limit": 5,
    }
    mock_sanitize.assert_not_awaited()
    mock_embed.assert_not_awaited()
    ctx._mock_sess.add.assert_not_called()


@pytest.mark.asyncio
async def test_remember_rejects_when_person_memory_ceiling_reached():
    """Per-person memory ceilings stop additional writes with a visible status."""
    from thenetwork.agent.tools import remember

    class FakeExecResult:
        def all(self):
            return [MagicMock()]

    ctx = FakeCtx()
    ctx.deps.settings.person_memory_limit = 1
    ctx._mock_sess.exec.return_value = FakeExecResult()

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock) as mock_embed, \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock) as mock_sanitize:
        result = await remember(ctx, text="Alice is an ML engineer", refs=["user-alice"])

    assert result == {
        "status": "error",
        "reason": "person_memory_limit_exceeded",
        "person_id": "user-alice",
        "limit": 1,
    }
    mock_sanitize.assert_not_awaited()
    mock_embed.assert_not_awaited()
    ctx._mock_sess.add.assert_not_called()


@pytest.mark.asyncio
async def test_remember_returns_empty_consolidation_candidates():
    """remember() always returns a bounded consolidation_candidates list."""
    from thenetwork.agent.tools import remember

    ctx = FakeCtx()
    added: list[object] = []
    ctx._mock_sess.add.side_effect = added.append
    sanitized = "[name] is an ml engineer"

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock, return_value=sanitized), \
         patch("thenetwork.agent.tools.match_memories", return_value=[]) as mock_match:
        result = await remember(
            ctx,
            text="Alice Smith is an ML engineer at Acme Corp, alice@acme.com",
            refs=["user-alice"],
        )

    assert result == {
        "memory_id": added[0].id,
        "consolidation_candidates": [],
    }
    mock_match.assert_called_once()
    assert mock_match.call_args.kwargs["exclude_memory_id"] == added[0].id


@pytest.mark.asyncio
async def test_remember_returns_sealed_duplicate_consolidation_candidates():
    """Consolidation candidates expose only memory IDs, gists, and scores."""
    from thenetwork.agent.tools import remember

    ctx = FakeCtx()
    added: list[object] = []
    ctx._mock_sess.add.side_effect = added.append
    raw_other_person_text = (
        "Bob Stone can be reached at bob.secret@example.com and researches privacy."
    )

    def fake_match_memories(_query_vec, _session, *, limit, exclude_memory_id):
        return [
            MemoryMatch(
                memory_id=exclude_memory_id,
                person_id="new-person-id",
                gist="newly stored memory",
                similarity=1.0,
            ),
            MemoryMatch(
                memory_id="old-memory-1",
                person_id="other-person-id",
                gist="[name] researches privacy.",
                similarity=0.9819,
            ),
            MemoryMatch(
                memory_id="old-memory-2",
                person_id="another-person-id",
                gist="[name] works on ML systems.",
                similarity=0.8765,
            ),
            MemoryMatch(
                memory_id="old-memory-3",
                person_id="third-person-id",
                gist="[name] is seeking privacy collaborators.",
                similarity=0.7654,
            ),
            MemoryMatch(
                memory_id="old-memory-4",
                person_id="fourth-person-id",
                gist="[name] should be trimmed by the bound.",
                similarity=0.6543,
            ),
        ]

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock, return_value="[name] researches privacy."), \
         patch("thenetwork.agent.tools.match_memories", side_effect=fake_match_memories):
        result = await remember(
            ctx,
            text=raw_other_person_text,
            refs=["user-bob"],
        )

    assert result["memory_id"] == added[0].id
    candidates = result["consolidation_candidates"]
    assert len(candidates) == 3
    assert [c["memory_id"] for c in candidates] == [
        "old-memory-1",
        "old-memory-2",
        "old-memory-3",
    ]
    assert candidates[0]["score"] == 0.982
    for candidate in candidates:
        assert set(candidate) == {"memory_id", "gist", "score"}
        assert "person_id" not in candidate
        assert "similarity" not in candidate
        assert "text" not in candidate
        assert "name" not in candidate
        assert "email" not in candidate

    serialized = repr(result)
    assert raw_other_person_text not in serialized
    assert "Bob Stone" not in serialized
    assert "bob.secret@example.com" not in serialized


@pytest.mark.asyncio
async def test_remember_dedupes_consolidation_candidates_by_memory_id():
    """A multi-ref memory returns one MemoryMatch row per ref; remember()
    must collapse those into a single candidate rather than surfacing the
    same memory_id repeatedly and crowding out a distinct candidate.
    """
    from thenetwork.agent.tools import MAX_CONSOLIDATION_CANDIDATES, remember

    ctx = FakeCtx()
    added: list[object] = []
    ctx._mock_sess.add.side_effect = added.append

    def fake_match_memories(_query_vec, _session, *, limit, exclude_memory_id):
        return [
            # a 2-ref memory: match_memories attributes it once per ref,
            # so it shows up twice with the same memory_id/gist/score.
            MemoryMatch(
                memory_id="intro-memory",
                person_id="person-a",
                gist="[name] introduced [name] to [name].",
                similarity=0.95,
            ),
            MemoryMatch(
                memory_id="intro-memory",
                person_id="person-b",
                gist="[name] introduced [name] to [name].",
                similarity=0.95,
            ),
            MemoryMatch(
                memory_id="other-memory",
                person_id="person-c",
                gist="[name] is looking for a cofounder.",
                similarity=0.80,
            ),
        ]

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.sanitize_memory_high_fidelity", new_callable=AsyncMock, return_value="[name] is a cofounder."), \
         patch("thenetwork.agent.tools.match_memories", side_effect=fake_match_memories):
        result = await remember(
            ctx,
            text="Alice is a cofounder",
            refs=["user-alice"],
        )

    candidates = result["consolidation_candidates"]
    memory_ids = [c["memory_id"] for c in candidates]
    assert memory_ids == sorted(set(memory_ids), key=memory_ids.index)
    assert memory_ids == ["intro-memory", "other-memory"]
    assert len(candidates) <= MAX_CONSOLIDATION_CANDIDATES
    assert added[0].id not in [c["memory_id"] for c in candidates]


@pytest.mark.asyncio
async def test_search_returns_gist_not_raw_text():
    """search() results must not include raw memory text - only gist (PII-stripped)."""
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
async def test_search_result_keys_sealed_for_other_people():
    """Cross-user search results must not expose anything beyond gist + opaque person id."""
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


@pytest.mark.asyncio
async def test_search_includes_memory_id_only_for_sender_owned_results():
    """The agent may receive self memory IDs so sender-requested deletion can work."""
    from thenetwork.agent.tools import search

    matches = [
        MemoryMatch(
            memory_id="self-memory",
            person_id="user-alice",
            gist="backend engineer",
            similarity=0.91,
        ),
        MemoryMatch(
            memory_id="other-memory",
            person_id="user-bob",
            gist="systems engineer",
            similarity=0.88,
        ),
    ]

    ctx = FakeCtx(sender_user_id="user-alice")
    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock, return_value=[0.0] * 1536), \
         patch("thenetwork.agent.tools.match_memories", return_value=matches):
        results = await search(ctx, query="my stored facts")

    assert results[0]["memory_id"] == "self-memory"
    assert "memory_id" not in results[1]


@pytest.mark.asyncio
async def test_forget_deletes_only_sender_owned_memory():
    from thenetwork.agent.tools import forget
    from thenetwork.db.models import Memory

    ctx = FakeCtx(sender_user_id="user-alice")
    memory = Memory(id="self-memory", text="Alice works on compilers", refs=["user-alice"])
    ctx._mock_sess.get.return_value = memory

    result = await forget(ctx, "self-memory")

    assert result == {"status": "deleted"}
    ctx._mock_sess.delete.assert_called_once_with(memory)
    ctx._mock_sess.commit.assert_called_once()


@pytest.mark.asyncio
async def test_forget_rejects_memory_not_owned_only_by_sender():
    from thenetwork.agent.tools import forget
    from thenetwork.db.models import Memory

    ctx = FakeCtx(sender_user_id="user-alice")
    memory = Memory(
        id="other-memory",
        text="Bob works on compilers",
        refs=["user-bob"],
    )
    ctx._mock_sess.get.return_value = memory

    result = await forget(ctx, "other-memory")

    assert result == {"status": "forbidden", "reason": "not_sender_memory"}
    ctx._mock_sess.delete.assert_not_called()
    ctx._mock_sess.commit.assert_not_called()


@pytest.mark.asyncio
async def test_search_rejects_query_over_configured_cap():
    """Oversized search queries must fail before embedding or retrieval."""
    from thenetwork.agent.tools import search

    ctx = FakeCtx()
    ctx.deps.settings.search_query_max_chars = 5

    with patch("thenetwork.agent.tools.embed_text", new_callable=AsyncMock) as mock_embed, \
         pytest.raises(ValueError, match="length cap"):
        await search(ctx, query="too long")

    mock_embed.assert_not_awaited()


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
         patch("smtplib.SMTP") as mock_smtp, \
         patch("thenetwork.email.outbound.MailBox") as mock_mailbox:
        s = MagicMock()
        s.smtp_host = "smtp.example.com"
        s.smtp_port = 587
        s.imap_account = "agent@example.com"
        s.imap_password = "secret"
        s.smtp_account = "agent@example.com"
        s.smtp_password = "secret"
        s.email_from = "agent@example.com"
        s.imap_host = "imap.example.com"
        s.imap_port = 993
        s.imap_sent_folder = "Sent"
        mock_settings.return_value = s

        smtp_instance = MagicMock()
        smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
        smtp_instance.__exit__ = MagicMock(return_value=False)
        smtp_instance.send_message.side_effect = fake_send_message
        mock_smtp.return_value = smtp_instance

        mb_instance = MagicMock()
        mb_instance.__enter__ = MagicMock(return_value=mb_instance)
        mb_instance.__exit__ = MagicMock(return_value=False)
        mock_mailbox.return_value.login.return_value = mb_instance

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

    sender_count = 0

    def fake_hit(limit, key):
        nonlocal sender_count
        if key == "authenticated-sender:flood@attacker.com":
            sender_count += 1
            return sender_count <= 10  # allow first 10, block 11th
        return True

    mock_limiter = MagicMock()
    mock_limiter.test.return_value = True
    mock_limiter.hit.side_effect = fake_hit
    mock_storage = MagicMock()
    mock_storage.check.return_value = True

    with patch("thenetwork.security.rate_limit._get_limiter", return_value=(mock_limiter, mock_storage)):
        results = [
            check_rate_limit("flood@attacker.com", sender_authenticated=True)
            for _ in range(12)
        ]

    assert all(results[:10])   # first 10 allowed
    assert not results[10]     # 11th blocked
    assert not results[11]     # 12th blocked
