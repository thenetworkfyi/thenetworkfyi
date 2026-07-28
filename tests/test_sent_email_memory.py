from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.agent.deps import AgentCapabilities, AgentDeps
from thenetwork.agent.tools import reply_to_sender, send_outreach
from thenetwork.audit import LOGGER_NAME, audit_event
from thenetwork.db.models import Memory, Person
from thenetwork.memory.recent_context import load_recent_sender_memory_context
from thenetwork.memory.sent_email import (
    SENT_EMAIL_SUMMARY_MAX_CHARS,
    SentEmailMemory,
    record_sent_email_memory,
)
from thenetwork.settings import Settings


class FakeSession:
    def __init__(self, *, memory_count: int = 0, recipient_exists: bool = True):
        self.memory_count = memory_count
        self.recipient_exists = recipient_exists
        self.added: list[Memory] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def exec(self, _query):
        return self

    def get(self, model, person_id):
        assert model is Person
        if not self.recipient_exists:
            return None
        return Person(
            id=person_id, name="Registered recipient", email="registered@example.com"
        )

    def one(self):
        return self.memory_count

    def add(self, memory):
        self.added.append(memory)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _settings(**overrides) -> Settings:
    return Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
        **overrides,
    )


@pytest.mark.parametrize(
    ("outcome", "reason", "error_type"),
    [
        ("success", None, None),
        ("blocked", "invalid_summary", None),
        ("blocked", "memory_text_too_long", None),
        ("blocked", "recipient_not_found", None),
        ("blocked", "person_memory_limit_exceeded", None),
        ("error", None, "RuntimeError"),
    ],
)
def test_sent_email_audit_preserves_bounded_categories(
    caplog,
    outcome,
    reason,
    error_type,
):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    fields = {
        "action": "insert",
        "record_type": "sent_email_memory",
        "refs_count": 1,
        "outcome": outcome,
    }
    if reason is not None:
        fields["reason"] = reason
    if error_type is not None:
        fields["error_type"] = error_type

    audit_event("database.action", **fields)

    event = json.loads(caplog.records[0].message)
    assert event["record_type"] == "sent_email_memory"
    assert event["outcome"] == outcome
    if reason is not None:
        assert event["reason"] == reason
    if error_type is not None:
        assert event["error_type"] == error_type


@pytest.mark.asyncio
async def test_successful_delivery_persists_one_sealed_summary_memory(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    session = FakeSession()
    private_subject = "Re: Confidential acquisition"
    private_body = "The complete message body must never be stored"
    recipient_address = "alice.private@example.com"
    summary = "a concise answer about the requested introduction criteria"

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory",
            new=MagicMock(return_value="a sealed sent-email purpose"),
        ) as sanitize,
        patch(
            "thenetwork.memory.sent_email.embed_text",
            new=AsyncMock(return_value=[0.25] * 1536),
        ) as embed,
    ):
        recorded = await record_sent_email_memory(
            SentEmailMemory("person-alice", summary),
            session_factory=lambda: session,
            settings=_settings(),
        )

    assert recorded is True
    assert session.commits == 1
    assert len(session.added) == 1
    memory = session.added[0]
    assert memory.text == f"Sent email: {summary}"
    assert memory.refs == ["person-alice"]
    assert memory.gist == "a sealed sent-email purpose"
    assert memory.embedding == [0.25] * 1536
    sanitize.assert_called_once_with(memory, session)
    embed.assert_awaited_once_with("a sealed sent-email purpose")
    serialized = repr(memory)
    assert private_subject not in serialized
    assert private_body not in serialized
    assert recipient_address not in serialized
    audit_json = "\n".join(record.message for record in caplog.records)
    assert summary not in audit_json
    assert private_subject not in audit_json
    assert private_body not in audit_json
    assert recipient_address not in audit_json
    audit_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message)["event"] == "database.action"
    ]
    assert audit_events[-1]["record_type"] == "sent_email_memory"
    assert audit_events[-1]["outcome"] == "success"


@pytest.mark.asyncio
async def test_summary_is_bounded_before_memory_persistence():
    session = FakeSession()

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory",
            new=MagicMock(return_value="sealed"),
        ),
        patch(
            "thenetwork.memory.sent_email.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
    ):
        recorded = await record_sent_email_memory(
            SentEmailMemory("person-alice", "purpose " * 1_000),
            session_factory=lambda: session,
            settings=_settings(),
        )

    assert recorded is True
    assert (
        len(session.added[0].text) == len("Sent email: ") + SENT_EMAIL_SUMMARY_MAX_CHARS
    )


@pytest.mark.asyncio
async def test_person_memory_limit_blocks_sent_memory_without_side_effects():
    session = FakeSession(memory_count=2)

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory",
            new_callable=MagicMock,
        ) as sanitize,
        patch(
            "thenetwork.memory.sent_email.embed_text",
            new_callable=AsyncMock,
        ) as embed,
    ):
        recorded = await record_sent_email_memory(
            SentEmailMemory("person-alice", "a relevant update"),
            session_factory=lambda: session,
            settings=_settings(person_memory_limit=2),
        )

    assert recorded is False
    assert session.added == []
    assert session.commits == 0
    sanitize.assert_not_called()
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unregistered_recipient_cannot_receive_a_sent_email_memory():
    session = FakeSession(recipient_exists=False)

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory",
            new_callable=MagicMock,
        ) as sanitize,
        patch(
            "thenetwork.memory.sent_email.embed_text",
            new_callable=AsyncMock,
        ) as embed,
    ):
        recorded = await record_sent_email_memory(
            SentEmailMemory("unknown-person", "join and usage instructions"),
            session_factory=lambda: session,
            settings=_settings(),
        )

    assert recorded is False
    assert session.added == []
    sanitize.assert_not_called()
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_failure_rolls_back_and_never_raises():
    session = FakeSession()

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory",
            new=MagicMock(side_effect=RuntimeError("sanitizer unavailable")),
        ),
        patch(
            "thenetwork.memory.sent_email.embed_text",
            new_callable=AsyncMock,
        ) as embed,
    ):
        recorded = await record_sent_email_memory(
            SentEmailMemory("person-alice", "a relevant update"),
            session_factory=lambda: session,
            settings=_settings(),
        )

    assert recorded is False
    assert session.commits == 0
    assert session.rollbacks == 1
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_delivery_uses_only_sealed_gist_in_deterministic_summary():
    from thenetwork.agent.tools import _send_event_fyi

    ctx = MagicMock()
    sealed_gist = "a small compiler engineering circle"
    with patch(
        "thenetwork.agent.tools._dispatch_email",
        new_callable=AsyncMock,
        return_value={"status": "sent"},
    ) as dispatch:
        result = await _send_event_fyi(
            ctx,
            recipient_user_id="person-bob",
            event_gist=sealed_gist,
            notice=MagicMock(value="first notice"),
        )

    assert result == {"status": "sent"}
    assert dispatch.await_args.kwargs["recipient_user_id"] == "person-bob"
    assert dispatch.await_args.kwargs["sent_email_summary"] == (
        f"an event recommendation about {sealed_gist}"
    )


class StoreResult:
    def __init__(self, *, one=None, values=None):
        self.one_value = one
        self.values = values or []

    def one(self):
        return self.one_value

    def all(self):
        return self.values


class MemoryStoreSession:
    def __init__(self):
        self.people = {
            "person-alice": Person(
                id="person-alice",
                name="Alice",
                email="alice@example.com",
            ),
            "person-bob": Person(
                id="person-bob",
                name="Bob",
                email="bob@example.com",
            ),
            "person-carol": Person(
                id="person-carol",
                name="Carol",
                email="carol@example.com",
            ),
        }
        self.memories: list[Memory] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, record_id):
        if model is Person:
            return self.people.get(record_id)
        raise AssertionError(f"unexpected model lookup: {model}")

    def exec(self, query):
        sql = str(query)
        params = query.compile().params
        person_id = next(
            (
                value[0]
                for value in params.values()
                if isinstance(value, list) and len(value) == 1
            ),
            None,
        )
        owned = [
            memory
            for memory in self.memories
            if person_id is not None and person_id in memory.refs
        ]
        if "count(" in sql.lower():
            return StoreResult(one=len(owned))
        if "memories.gist" in sql:
            limit = next(
                (value for value in params.values() if isinstance(value, int)),
                len(owned),
            )
            ordered = sorted(
                owned,
                key=lambda memory: (memory.created_at, memory.id),
                reverse=True,
            )
            return StoreResult(
                values=[memory.gist for memory in ordered[:limit] if memory.gist]
            )
        raise AssertionError(f"unexpected query: {sql}")

    def add(self, memory):
        self.memories.append(memory)

    def commit(self):
        return None

    def rollback(self):
        return None


def _tool_context(
    store: MemoryStoreSession,
    *,
    sender_id: str,
    capabilities: AgentCapabilities | None = None,
):
    return SimpleNamespace(
        deps=AgentDeps(
            capabilities=capabilities
            if capabilities is not None
            else AgentCapabilities(),
            settings=_settings(
                dispatch_recipient_daily_cap=99,
                dispatch_sender_reply_daily_cap=99,
            ),
            sender_email=store.people[sender_id].email,
            sender_user_id=sender_id,
            sender_authenticated=True,
            session_factory=lambda: store,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_kind", ["reply", "outreach"])
async def test_successful_agent_delivery_is_injected_into_the_recipient_next_run(
    delivery_kind,
):
    from thenetwork.agent.core import run_agent_for_email

    store = MemoryStoreSession()
    sender_id = "person-alice"
    recipient_id = "person-alice" if delivery_kind == "reply" else "person-bob"
    ctx = _tool_context(
        store,
        sender_id=sender_id,
        capabilities=AgentCapabilities(send_reply=MagicMock()),
    )
    summary = "an answer about a specific manufacturing introduction"

    def sanitize(memory, _session):
        return memory.text

    with (
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap"),
        patch(
            "thenetwork.memory.sent_email.sanitize_memory",
            new=MagicMock(side_effect=sanitize),
        ),
        patch(
            "thenetwork.memory.sent_email.embed_text",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
    ):
        if delivery_kind == "reply":
            result = await reply_to_sender(
                ctx,
                subject="Private subject",
                body_text="Private body",
                sent_email_summary=summary,
            )
        else:
            result = await send_outreach(
                ctx,
                recipient_user_id=recipient_id,
                subject="Private subject",
                body_text="Private body",
                sent_email_summary=summary,
            )

    assert result == {"status": "sent"}
    assert len(store.memories) == 1

    fake_result = SimpleNamespace(output="", all_messages=lambda: [])
    fake_agent = SimpleNamespace(run=AsyncMock(return_value=fake_result))
    with (
        patch("thenetwork.agent.core.get_settings", return_value=_settings()),
        patch("thenetwork.agent.core.build_agent", return_value=fake_agent),
    ):
        await run_agent_for_email(
            sender_email=store.people[recipient_id].email,
            sender_user_id=recipient_id,
            email_subject="Later message",
            email_body="What happened earlier?",
            session_factory=lambda: store,
        )

    user_message = fake_agent.run.await_args.args[0]
    assert f"Sent email: {summary}" in user_message
    assert "Private subject" not in user_message
    assert "Private body" not in user_message
    assert store.people[recipient_id].email not in user_message

    other_context = load_recent_sender_memory_context(
        "person-carol",
        session_factory=lambda: store,
    )
    assert other_context.text == ""


@pytest.mark.asyncio
async def test_failed_agent_delivery_is_absent_from_later_context():
    store = MemoryStoreSession()
    ctx = _tool_context(
        store,
        sender_id="person-alice",
        capabilities=AgentCapabilities(
            send_reply=MagicMock(side_effect=RuntimeError("smtp unavailable"))
        ),
    )

    with patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True):
        with pytest.raises(RuntimeError, match="smtp unavailable"):
            await send_outreach(
                ctx,
                recipient_user_id="person-bob",
                subject="Private subject",
                body_text="Private body",
                sent_email_summary="a relevant update",
            )

    assert store.memories == []
    context = load_recent_sender_memory_context(
        "person-bob",
        session_factory=lambda: store,
    )
    assert context == load_recent_sender_memory_context(
        None,
        session_factory=lambda: store,
    )
