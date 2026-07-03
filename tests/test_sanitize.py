from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _FakeRecognizerResult:
    entity_type: str
    start: int
    end: int


class _FakeAnalyzer:
    """Stands in for presidio_analyzer.AnalyzerEngine in tests.

    Real Presidio needs a downloaded spacy model, which the sandboxed test
    environment can't fetch, so these tests exercise the redaction logic
    (span lookup + right-to-left substitution) against a fake analyzer with
    the same `.analyze(text=..., entities=..., language=...) -> [result]`
    surface, and separately prove the ImportError fallback path.
    """

    def __init__(self, results: list[_FakeRecognizerResult]) -> None:
        self._results = results

    def analyze(self, text: str, entities: list[str], language: str) -> list[_FakeRecognizerResult]:
        return self._results


def test_sanitize_memory_redacts_names_orgs_locations_when_presidio_available(monkeypatch):
    text = "Alice Smith works at Acme Corp in Berlin. Email alice@example.com or call 415-555-0199."
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()

    fake_results = [
        _FakeRecognizerResult("PERSON", text.index("Alice Smith"), text.index("Alice Smith") + len("Alice Smith")),
        _FakeRecognizerResult("ORGANIZATION", text.index("Acme Corp"), text.index("Acme Corp") + len("Acme Corp")),
        _FakeRecognizerResult("LOCATION", text.index("Berlin"), text.index("Berlin") + len("Berlin")),
    ]
    monkeypatch.setattr(
        sanitize_mod, "_get_presidio_analyzer", lambda: _FakeAnalyzer(fake_results)
    )

    result = sanitize_mod.sanitize_memory(memory, session)

    assert result == "[name] works at [org] in [location]. Email [email] or call [phone]."
    assert memory.gist == result
    assert "Alice" not in result
    assert "Acme Corp" not in result
    assert "Berlin" not in result
    assert "alice@example.com" not in result
    assert "415-555-0199" not in result


def test_sanitize_memory_falls_back_to_regex_only_when_presidio_unavailable(monkeypatch):
    text = "Alice Smith works at Acme Corp in Berlin. Email alice@example.com or call 415-555-0199."
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()

    monkeypatch.setattr(sanitize_mod, "_get_presidio_analyzer", lambda: None)

    result = sanitize_mod.sanitize_memory(memory, session)

    assert result == "Alice Smith works at Acme Corp in Berlin. Email [email] or call [phone]."
    assert memory.gist == result


def test_get_presidio_analyzer_returns_none_when_not_installed(monkeypatch):
    sanitize_mod._get_presidio_analyzer.cache_clear()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "presidio_analyzer":
            raise ImportError("no module named presidio_analyzer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    try:
        assert sanitize_mod._get_presidio_analyzer() is None
    finally:
        sanitize_mod._get_presidio_analyzer.cache_clear()


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
    # Pin the deterministic fallback to its regex-only behavior regardless of
    # whether Presidio happens to be installed in the environment running
    # this test — the NER-strengthened path is covered separately below.
    monkeypatch.setattr(sanitize_mod, "_get_presidio_analyzer", lambda: None)

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
