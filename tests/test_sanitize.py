from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from thenetwork.db.models import Memory
from thenetwork.memory import sanitize as sanitize_mod


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Memory] = []
        self.flushes = 0

    def add(self, memory: Memory) -> None:
        self.added.append(memory)

    def flush(self) -> None:
        self.flushes += 1


@pytest.mark.asyncio
async def test_high_fidelity_sanitizer_uses_llm_output(monkeypatch):
    memory = Memory(
        text=(
            "Alice Smith can mentor founders. Email alice@example.com, "
            "call 415-555-0199, or meet at 123 Main Street."
        ),
        refs=["person-1"],
    )
    session = FakeSession()
    sanitized = (
        "[name] can mentor founders. Email [email], call [phone], "
        "or meet at [address]."
    )

    async def fake_llm(mem: Memory, sess: FakeSession) -> str:
        mem.gist = sanitized
        sess.add(mem)
        sess.flush()
        return sanitized

    mock_llm = AsyncMock(side_effect=fake_llm)
    monkeypatch.setattr(sanitize_mod, "sanitize_memory_llm", mock_llm)

    result = await sanitize_mod.sanitize_memory_high_fidelity(memory, session)

    assert result == sanitized
    assert memory.gist == sanitized
    assert "Alice" not in result
    assert "alice@example.com" not in result
    assert "415-555-0199" not in result
    assert "123 Main Street" not in result
    assert session.added == [memory]
    assert session.flushes == 1
    mock_llm.assert_awaited_once_with(memory, session)


@pytest.mark.asyncio
async def test_high_fidelity_sanitizer_falls_back_to_deterministic_strip(monkeypatch):
    raw_text = (
        "Alice Smith lives at 123 Main Street and can discuss compilers "
        "via alice@example.com or 415-555-0199."
    )
    memory = Memory(text=raw_text, refs=["person-1"])
    session = FakeSession()

    async def fail_llm(_memory: Memory, _session: FakeSession) -> str:
        raise RuntimeError(f"provider failed while handling: {raw_text}")

    mock_llm = AsyncMock(side_effect=fail_llm)
    monkeypatch.setattr(sanitize_mod, "sanitize_memory_llm", mock_llm)

    result = await sanitize_mod.sanitize_memory_high_fidelity(memory, session)

    assert result == (
        "Alice Smith lives at 123 Main Street and can discuss compilers "
        "via [email] or [phone]."
    )
    assert memory.gist == result
    assert session.added == [memory]
    assert session.flushes == 1
    mock_llm.assert_awaited_once_with(memory, session)


@pytest.mark.asyncio
async def test_llm_sanitizer_uses_fixed_no_tools_prompt(monkeypatch):
    memory = Memory(
        text=(
            "Dana Jones wants climate hardware intros. Reach dana@example.com, "
            "call 212-555-1212, or visit 7 Market Street."
        ),
        refs=["person-1"],
    )
    session = FakeSession()
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        async def run(self, prompt: str) -> SimpleNamespace:
            captured["prompt"] = prompt
            return SimpleNamespace(
                output=(
                    "[name] wants climate hardware intros. Reach [email], "
                    "call [phone], or visit [address]."
                )
            )

    monkeypatch.setattr("pydantic_ai.Agent", FakeAgent)
    monkeypatch.setattr(
        "thenetwork.settings.get_settings",
        lambda: SimpleNamespace(agent_model="test:model"),
    )

    result = await sanitize_mod.sanitize_memory_llm(memory, session)

    assert result == (
        "[name] wants climate hardware intros. Reach [email], "
        "call [phone], or visit [address]."
    )
    assert memory.gist == result
    assert session.added == [memory]
    assert session.flushes == 1
    assert captured["args"] == ()
    kwargs = captured["kwargs"]
    assert kwargs["model"] == "test:model"
    assert kwargs["output_type"] is str
    assert "tools" not in kwargs
    assert "names" in kwargs["system_prompt"]
    assert "email addresses" in kwargs["system_prompt"]
    assert "phone numbers" in kwargs["system_prompt"]
    assert "specific street addresses" in kwargs["system_prompt"]
    assert "Return only the sanitized text" in kwargs["system_prompt"]
    assert captured["prompt"] == memory.text
    assert "Dana Jones" not in result
    assert "dana@example.com" not in result
    assert "212-555-1212" not in result
    assert "7 Market Street" not in result
