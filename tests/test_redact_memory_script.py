from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from thenetwork.db.models import Memory
from thenetwork.scripts.redact_memory import build_parser, main, redact_memory_record


class FakeRedactSession:
    def __init__(self, memory: Memory | None = None):
        self.memory = memory
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def get(self, model, memory_id):
        if self.memory and self.memory.id == memory_id:
            return self.memory
        return None

    def add(self, entity):
        pass

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_redact_memory_dry_run():
    mem = Memory(
        id="mem-1",
        text="John Doe at john@example.com called 555-1234",
        refs=["person-1"],
        gist="John Doe at john@example.com called 555-1234",
    )
    session = FakeRedactSession(mem)

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_text",
            return_value="[NAME] at [EMAIL] called [PHONE]",
        ),
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            new=AsyncMock(return_value="[NAME] at [EMAIL] called [PHONE]"),
        ),
        patch(
            "thenetwork.scripts.redact_memory.embed_text",
            new=AsyncMock(return_value=[0.1] * 1536),
        ),
    ):
        summary = await redact_memory_record("mem-1", session, commit=False)

    assert summary["memory_id"] == "mem-1"
    assert summary["committed"] is False
    assert summary["text_changed"] is True
    assert summary["new_text"] == "[NAME] at [EMAIL] called [PHONE]"
    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_redact_memory_commit():
    mem = Memory(
        id="mem-2",
        text="Contact Alice Smith at 555-9999",
        refs=["person-2"],
        gist="Contact Alice Smith at 555-9999",
    )
    session = FakeRedactSession(mem)

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_text",
            return_value="Contact [NAME] at [PHONE]",
        ),
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            new=AsyncMock(return_value="Contact [NAME] at [PHONE]"),
        ),
        patch(
            "thenetwork.scripts.redact_memory.embed_text",
            new=AsyncMock(return_value=[0.2] * 1536),
        ),
    ):
        summary = await redact_memory_record("mem-2", session, commit=True)

    assert summary["memory_id"] == "mem-2"
    assert summary["committed"] is True
    assert summary["text_changed"] is True
    assert mem.text == "Contact [NAME] at [PHONE]"
    assert mem.embedding == [0.2] * 1536
    assert session.committed is True


@pytest.mark.asyncio
async def test_redact_memory_specific_string():
    mem = Memory(
        id="mem-3",
        text="Secret project Codename Omega in Fargo",
        refs=[],
        gist=None,
    )
    session = FakeRedactSession(mem)

    with patch(
        "thenetwork.scripts.redact_memory.embed_text",
        new=AsyncMock(return_value=[0.3] * 1536),
    ):
        summary = await redact_memory_record(
            "mem-3",
            session,
            string_to_redact="Codename Omega",
            replacement="[REDACTED PROJECT]",
            commit=True,
        )

    assert summary["new_text"] == "Secret project [REDACTED PROJECT] in Fargo"
    assert mem.text == "Secret project [REDACTED PROJECT] in Fargo"
    assert session.committed is True


@pytest.mark.asyncio
async def test_redact_memory_regex_pattern():
    mem = Memory(
        id="mem-4",
        text="Internal ID: ABC-12345 secret data",
        refs=[],
        gist=None,
    )
    session = FakeRedactSession(mem)

    with patch(
        "thenetwork.scripts.redact_memory.embed_text",
        new=AsyncMock(return_value=[0.4] * 1536),
    ):
        summary = await redact_memory_record(
            "mem-4",
            session,
            pattern_to_redact=r"ABC-\d+",
            replacement="[ID REDACTED]",
            commit=True,
        )

    assert summary["new_text"] == "Internal ID: [ID REDACTED] secret data"
    assert mem.text == "Internal ID: [ID REDACTED] secret data"
    assert session.committed is True


@pytest.mark.asyncio
async def test_redact_memory_not_found():
    session = FakeRedactSession(None)
    with pytest.raises(ValueError, match="Memory not found: mem-missing"):
        await redact_memory_record("mem-missing", session)


def test_build_parser():
    parser = build_parser()
    args = parser.parse_args(
        ["mem-123", "--commit", "--string", "secret", "--replacement", "XXX"]
    )
    assert args.memory_id == "mem-123"
    assert args.commit is True
    assert args.string_to_redact == "secret"
    assert args.replacement == "XXX"


def test_cli_main_not_found(capsys):
    session = FakeRedactSession(None)

    @contextmanager
    def fake_get_session():
        yield session

    with (
        patch(
            "thenetwork.scripts.redact_memory.get_session", side_effect=fake_get_session
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(["mem-not-found"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "Error: Memory not found: mem-not-found" in captured.err
