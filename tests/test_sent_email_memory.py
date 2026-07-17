from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.db.models import Memory, Person
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


@pytest.mark.asyncio
async def test_successful_delivery_persists_one_sealed_summary_memory():
    session = FakeSession()
    private_subject = "Re: Confidential acquisition"
    private_body = "The complete message body must never be stored"
    recipient_address = "alice.private@example.com"
    summary = "a concise answer about the requested introduction criteria"

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory_high_fidelity",
            new=AsyncMock(return_value="a sealed sent-email purpose"),
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
    sanitize.assert_awaited_once_with(memory, session)
    embed.assert_awaited_once_with("a sealed sent-email purpose")
    serialized = repr(memory)
    assert private_subject not in serialized
    assert private_body not in serialized
    assert recipient_address not in serialized


@pytest.mark.asyncio
async def test_summary_is_bounded_before_memory_persistence():
    session = FakeSession()

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory_high_fidelity",
            new=AsyncMock(return_value="sealed"),
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
            "thenetwork.memory.sent_email.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
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
    sanitize.assert_not_awaited()
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unregistered_recipient_cannot_receive_a_sent_email_memory():
    session = FakeSession(recipient_exists=False)

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory_high_fidelity",
            new_callable=AsyncMock,
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
    sanitize.assert_not_awaited()
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_failure_rolls_back_and_never_raises():
    session = FakeSession()

    with (
        patch(
            "thenetwork.memory.sent_email.sanitize_memory_high_fidelity",
            new=AsyncMock(side_effect=RuntimeError("sanitizer unavailable")),
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
