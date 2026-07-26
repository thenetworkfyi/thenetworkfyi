from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from thenetwork.agent.deps import AgentDeps
from thenetwork.db.models import Memory
from thenetwork.llm_observability import LLMWorkload
from thenetwork.memory import sanitize as sanitize_mod
from thenetwork.settings import Settings


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
    surface, and separately prove the ImportError fail-loud path.
    """

    def __init__(self, results: list[_FakeRecognizerResult]) -> None:
        self._results = results

    def analyze(
        self, text: str, entities: list[str], language: str
    ) -> list[_FakeRecognizerResult]:
        return self._results


def test_sanitize_memory_redacts_names_emails_phones_with_presidio(monkeypatch):
    text = "Alice Smith works at Acme Corp in Berlin. Email alice@example.com or call 415-555-0199."
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()

    fake_results = [
        _FakeRecognizerResult(
            "PERSON",
            text.index("Alice Smith"),
            text.index("Alice Smith") + len("Alice Smith"),
        ),
        _FakeRecognizerResult(
            "EMAIL_ADDRESS",
            text.index("alice@example.com"),
            text.index("alice@example.com") + len("alice@example.com"),
        ),
        _FakeRecognizerResult(
            "PHONE_NUMBER",
            text.index("415-555-0199"),
            text.index("415-555-0199") + len("415-555-0199"),
        ),
    ]
    monkeypatch.setattr(
        sanitize_mod, "_get_presidio_analyzer", lambda: _FakeAnalyzer(fake_results)
    )

    result = sanitize_mod.sanitize_memory(memory, session)

    assert (
        result == "[name] works at Acme Corp in Berlin. Email [email] or call [phone]."
    )
    assert memory.gist == result
    assert "Alice" not in result
    assert "Acme Corp" in result
    assert "Berlin" in result
    assert "alice@example.com" not in result
    assert "415-555-0199" not in result


def test_sanitize_memory_fails_loud_when_presidio_unavailable(monkeypatch):
    text = "Alice Smith works at Acme Corp in Berlin. Email alice@example.com or call 415-555-0199."
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()

    def unavailable():
        raise RuntimeError("presidio missing")

    monkeypatch.setattr(sanitize_mod, "_get_presidio_analyzer", unavailable)

    with pytest.raises(RuntimeError, match="presidio missing"):
        sanitize_mod.sanitize_memory(memory, session)
    assert memory.gist is None
    assert session.added == []
    assert session.flushes == 0


@pytest.mark.integration
@pytest.mark.real_presidio
def test_sanitize_memory_redacts_with_real_presidio_analyzer():
    """Exercise the real Presidio analyzer path when its model is installed."""
    pytest.importorskip("presidio_analyzer")
    sanitize_mod._get_presidio_analyzer.cache_clear()
    try:
        analyzer = sanitize_mod._get_presidio_analyzer()
        if analyzer is None:
            pytest.skip(
                "real Presidio analyzer unavailable; install its local Spacy model"
            )

        text = "Alice Smith lives in Seattle. Email alice.smith@example.com or call 415-555-0199."
        memory = Memory(text=text, refs=["person-1"])
        session = FakeSession()

        result = sanitize_mod.sanitize_memory(memory, session)

        assert "[email]" in result
        assert "[phone]" in result
        assert "[name]" in result
        assert "Seattle" in result
        assert "Alice" not in result
        assert "alice.smith@example.com" not in result
        assert "415-555-0199" not in result
        assert memory.gist == result
        assert session.added == [memory]
        assert session.flushes == 1
    finally:
        sanitize_mod._get_presidio_analyzer.cache_clear()


class _FakeVocab:
    """Stands in for the spaCy vocab _is_handle_like consults.

    Real en_core_web_lg is an integration-only dependency here (see the
    _FakeAnalyzer docstring), so these tests pin the lexicon to a small known
    set and exercise the decision logic. `test_real_lexicon_*` covers the
    genuine model.
    """

    _KNOWN = {"senior", "engineer", "staff", "recruiter", "public", "mike", "lay"}

    def __getitem__(self, word: str) -> SimpleNamespace:
        return SimpleNamespace(is_oov=word.lower() not in self._KNOWN)


def _no_ner(monkeypatch) -> None:
    """Neutralize the NER pass so a test isolates the structural handle pass.

    A handle is not a Presidio entity at all, so proving it is stripped means
    proving the deterministic tier removes it with no NER help whatsoever.
    """
    monkeypatch.setattr(
        sanitize_mod, "_get_presidio_analyzer", lambda: _FakeAnalyzer([])
    )
    monkeypatch.setattr(sanitize_mod, "_english_vocab", _FakeVocab)


HANDLE_TEXTS: list[tuple[str, str, str]] = [
    ("label-colon", "Reach him on GitHub: mkly for the port.", "mkly"),
    ("label-equals-sigil", "twitter handle = @mkly these days.", "mkly"),
    ("label-is", "my discord is mkly if that helps.", "mkly"),
    ("bare-url", "Profile at github.com/mkly has the code.", "mkly"),
    (
        "scheme-url-path-prefix",
        "See https://www.linkedin.com/in/mike-lay for background.",
        "mike-lay",
    ),
    ("bare-sigil", "Ping @mkly in the channel.", "mkly"),
]


@pytest.mark.parametrize(
    "text,handle",
    [pytest.param(text, handle, id=case) for case, text, handle in HANDLE_TEXTS],
)
def test_sanitize_text_redacts_platform_handles_without_ner(monkeypatch, text, handle):
    """A handle must not survive the deterministic tier into a cross-user gist."""
    _no_ner(monkeypatch)

    gist = sanitize_mod.sanitize_text(text)

    assert handle not in gist, f"{handle!r} leaked into gist: {gist!r}"
    assert "[handle]" in gist


def test_sanitize_text_keeps_platform_and_host_for_search_recall(monkeypatch):
    _no_ner(monkeypatch)

    assert sanitize_mod.sanitize_text("GitHub: mkly") == "GitHub: [handle]"
    assert (
        sanitize_mod.sanitize_text("see github.com/mkly") == "see github.com/[handle]"
    )


def test_handle_redaction_is_idempotent(monkeypatch):
    """_validate_llm_gist rejects any candidate that sanitize_text would change.

    A non-idempotent handle pass would therefore reject every LLM-tier gist
    and silently downgrade the sanitizer to its deterministic output.
    """
    _no_ner(monkeypatch)

    for _, text, _handle in HANDLE_TEXTS:
        once = sanitize_mod.sanitize_text(text)
        assert sanitize_mod.sanitize_text(once) == once


def test_handle_pass_leaves_email_addresses_for_presidio(monkeypatch):
    """The @-sigil pattern must not chew up an email before the NER pass sees it."""
    text = "Email alice@example.com or bob.smith@sub.example.co.uk."

    assert sanitize_mod._strip_handles(text) == text


def test_sanitize_text_does_not_invent_handles_in_ordinary_prose(monkeypatch):
    _no_ner(monkeypatch)
    text = "She runs a bakery co-op and is looking for food-logistics contacts."

    assert sanitize_mod.sanitize_text(text) == text


PROSE_AFTER_LABEL_TEXTS = [
    pytest.param("LinkedIn: Senior Engineer at a co-op.", id="role-after-label"),
    pytest.param(
        "Her linkedin is public if you want context.", id="adjective-after-is"
    ),
    pytest.param("Slack: staff channel only.", id="common-noun-after-label"),
]


@pytest.mark.parametrize("text", PROSE_AFTER_LABEL_TEXTS)
def test_platform_label_does_not_redact_following_prose(monkeypatch, text):
    """A platform label is not proof the next word is a username.

    Redacting role and description words would corrupt the freeform content
    the gist is embedded for, which is the whole point of keeping
    organizations and locations in gists at all.
    """
    _no_ner(monkeypatch)

    assert sanitize_mod.sanitize_text(text) == text


def test_explicit_marker_redacts_even_a_dictionary_word(monkeypatch):
    """An explicit marker or sigil settles it; the lexicon is not consulted."""
    _no_ner(monkeypatch)

    assert sanitize_mod.sanitize_text("github username: staff") == (
        "github username: [handle]"
    )
    assert sanitize_mod.sanitize_text("github: @staff") == "github: [handle]"


@pytest.mark.integration
@pytest.mark.real_presidio
def test_real_lexicon_separates_handles_from_role_prose():
    """The same discrimination against the genuine en_core_web_lg vocab."""
    pytest.importorskip("presidio_analyzer")
    sanitize_mod._get_presidio_analyzer.cache_clear()
    sanitize_mod._english_vocab.cache_clear()
    try:
        assert sanitize_mod._is_handle_like("mkly", explicit=False) is True
        assert sanitize_mod._is_handle_like("senior", explicit=False) is False
        assert sanitize_mod._is_handle_like("engineer", explicit=False) is False

        text = "Contributes to open source (GitHub: mkly). LinkedIn: Senior Engineer."
        gist = sanitize_mod.sanitize_text(text)

        assert "mkly" not in gist
        assert "[handle]" in gist
        assert "Senior Engineer" in gist
    finally:
        sanitize_mod._get_presidio_analyzer.cache_clear()
        sanitize_mod._english_vocab.cache_clear()


def test_get_presidio_analyzer_raises_when_not_installed(monkeypatch):
    sanitize_mod._get_presidio_analyzer.cache_clear()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "presidio_analyzer":
            raise ImportError("no module named presidio_analyzer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    try:
        with pytest.raises(RuntimeError, match="presidio-analyzer is required"):
            sanitize_mod._get_presidio_analyzer()
    finally:
        sanitize_mod._get_presidio_analyzer.cache_clear()


def _enable_llm_tier(monkeypatch) -> None:
    """Point sanitize_mod's local `get_settings` import at a stub with the
    opt-in LLM tier switched on, matching the pattern of the fixed-prompt
    test below (sanitize_* functions re-import get_settings per call, so
    patching the module attribute is enough)."""
    monkeypatch.setattr(
        "thenetwork.settings.get_settings",
        lambda: SimpleNamespace(
            agent_model="test:model", sanitize_llm_tier_enabled=True
        ),
    )


@pytest.mark.asyncio
async def test_high_fidelity_sanitizer_uses_llm_output_when_tier_enabled(monkeypatch):
    memory = Memory(
        text=(
            "Alice Smith can mentor founders. Email alice@example.com, "
            "call 415-555-0199, or meet at 123 Main Street."
        ),
        refs=["person-1"],
    )
    session = FakeSession()
    sanitized = (
        "[name] can mentor founders. Email [email], call [phone], or meet at [address]."
    )
    _enable_llm_tier(monkeypatch)

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
async def test_high_fidelity_sanitizer_falls_back_to_deterministic_strip_on_llm_error(
    monkeypatch,
):
    raw_text = (
        "Alice Smith lives at 123 Main Street and can discuss compilers "
        "via alice@example.com or 415-555-0199."
    )
    memory = Memory(text=raw_text, refs=["person-1"])
    session = FakeSession()
    _enable_llm_tier(monkeypatch)

    async def fail_llm(_memory: Memory, _session: FakeSession) -> str:
        raise RuntimeError(f"provider failed while handling: {raw_text}")

    mock_llm = AsyncMock(side_effect=fail_llm)
    monkeypatch.setattr(sanitize_mod, "sanitize_memory_llm", mock_llm)
    mock_audit = MagicMock()
    monkeypatch.setattr(sanitize_mod, "audit_event", mock_audit)
    fake_results = [
        _FakeRecognizerResult(
            "PERSON",
            raw_text.index("Alice Smith"),
            raw_text.index("Alice Smith") + len("Alice Smith"),
        ),
        _FakeRecognizerResult(
            "EMAIL_ADDRESS",
            raw_text.index("alice@example.com"),
            raw_text.index("alice@example.com") + len("alice@example.com"),
        ),
        _FakeRecognizerResult(
            "PHONE_NUMBER",
            raw_text.index("415-555-0199"),
            raw_text.index("415-555-0199") + len("415-555-0199"),
        ),
    ]
    monkeypatch.setattr(
        sanitize_mod, "_get_presidio_analyzer", lambda: _FakeAnalyzer(fake_results)
    )

    result = await sanitize_mod.sanitize_memory_high_fidelity(memory, session)

    assert result == (
        "[name] lives at 123 Main Street and can discuss compilers "
        "via [email] or [phone]."
    )
    assert memory.gist == result
    assert session.added == [memory]
    assert session.flushes == 1
    mock_llm.assert_awaited_once_with(memory, session)
    mock_audit.assert_called_once_with(
        "sanitize.tier_downgrade", error_type="RuntimeError"
    )


@pytest.mark.asyncio
async def test_high_fidelity_sanitizer_skips_llm_when_tier_disabled(monkeypatch):
    """Default settings (sanitize_llm_tier_enabled=False) must never call the
    LLM sanitizer - the opt-in tier is off by default because it costs a
    model call and adds latency on every person-referencing write."""
    raw_text = (
        "Alice Smith lives in Berlin. Email alice@example.com or call 415-555-0199."
    )
    memory = Memory(text=raw_text, refs=["person-1"])
    session = FakeSession()

    monkeypatch.setattr(
        "thenetwork.settings.get_settings",
        lambda: SimpleNamespace(
            agent_model="test:model", sanitize_llm_tier_enabled=False
        ),
    )
    fake_results = [
        _FakeRecognizerResult(
            "PERSON",
            raw_text.index("Alice Smith"),
            raw_text.index("Alice Smith") + len("Alice Smith"),
        ),
        _FakeRecognizerResult(
            "EMAIL_ADDRESS",
            raw_text.index("alice@example.com"),
            raw_text.index("alice@example.com") + len("alice@example.com"),
        ),
        _FakeRecognizerResult(
            "PHONE_NUMBER",
            raw_text.index("415-555-0199"),
            raw_text.index("415-555-0199") + len("415-555-0199"),
        ),
    ]
    monkeypatch.setattr(
        sanitize_mod, "_get_presidio_analyzer", lambda: _FakeAnalyzer(fake_results)
    )
    mock_llm = AsyncMock(
        side_effect=AssertionError("LLM sanitizer must not run when tier is disabled")
    )
    monkeypatch.setattr(sanitize_mod, "sanitize_memory_llm", mock_llm)

    result = await sanitize_mod.sanitize_memory_high_fidelity(memory, session)

    mock_llm.assert_not_awaited()
    assert result == "[name] lives in Berlin. Email [email] or call [phone]."
    assert memory.gist == result


@pytest.mark.asyncio
async def test_llm_sanitizer_uses_fixed_no_tools_prompt(monkeypatch):
    """Also proves sanitize_memory_llm is built with settings.small_agent_model,
    not settings.agent_model: the fake settings stub sets only the former, and
    the `kwargs["model"]` assertion below fails if the sanitizer reads the
    wrong field."""
    memory = Memory(
        text=(
            "Dana Jones wants climate hardware intros. Reach dana@example.com, "
            "call 212-555-1212, or visit 7 Market Street."
        ),
        refs=["person-1"],
    )
    session = FakeSession()
    captured: dict[str, object] = {}
    deterministic = (
        "[name] wants climate hardware intros. Reach [email], "
        "call [phone], or visit 7 Market Street."
    )

    def deterministic_sanitize(text: str) -> str:
        return deterministic if text == memory.text else text

    monkeypatch.setattr(sanitize_mod, "sanitize_text", deterministic_sanitize)

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

    def resolve_model(model, api_key, timeout, *, workload):
        captured["workload"] = workload
        return model

    monkeypatch.setattr("thenetwork.model_config.model_with_api_key", resolve_model)
    monkeypatch.setattr(
        "thenetwork.settings.get_settings",
        lambda: SimpleNamespace(
            small_agent_model="test:model",
            small_agent_api_key="small-key",
            model_request_timeout_seconds=90.0,
        ),
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
    assert captured["workload"] is LLMWorkload.MEMORY_SANITIZER
    kwargs = captured["kwargs"]
    assert kwargs["model"] == "test:model"
    assert kwargs["output_type"] is str
    assert "tools" not in kwargs
    assert "names" in kwargs["system_prompt"]
    assert "email addresses" in kwargs["system_prompt"]
    assert "phone numbers" in kwargs["system_prompt"]
    assert "specific street addresses" in kwargs["system_prompt"]
    assert (
        "employers" in kwargs["system_prompt"]
        or "organizations" in kwargs["system_prompt"]
    )
    assert "social media handles" in kwargs["system_prompt"]
    assert "URLs" in kwargs["system_prompt"] or "links" in kwargs["system_prompt"]
    assert "quasi-identifying combinations" in kwargs["system_prompt"]
    assert "Return only the sanitized text" in kwargs["system_prompt"]
    assert captured["prompt"] == deterministic
    assert "Dana Jones" not in captured["prompt"]
    assert "dana@example.com" not in captured["prompt"]
    assert "212-555-1212" not in captured["prompt"]
    assert "Dana Jones" not in result
    assert "dana@example.com" not in result
    assert "212-555-1212" not in result
    assert "7 Market Street" not in result


@pytest.mark.asyncio
async def test_llm_sanitizer_prompt_contract_via_function_model(monkeypatch):
    """Exercise sanitize_memory_llm through a real pydantic-ai Agent wired to
    FunctionModel, so this proves the prompt/no-tools contract against the
    actual pydantic-ai message plumbing rather than a hand-rolled Agent
    double (matches the FunctionModel/TestModel convention used elsewhere,
    e.g. tests/scenarios/test_archetypes.py, tests/security/test_redteam.py)."""
    from pydantic_ai.messages import (
        ModelMessage,
        ModelResponse,
        SystemPromptPart,
        TextPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    memory = Memory(
        text="Erin Cole works at Globex and tweets @erincodes, see erin.dev.",
        refs=["person-1"],
    )
    session = FakeSession()
    captured: dict[str, object] = {}
    deterministic = "[name] works at Globex and tweets @erincodes, see erin.dev."

    def deterministic_sanitize(text: str) -> str:
        return deterministic if text == memory.text else text

    monkeypatch.setattr(sanitize_mod, "sanitize_text", deterministic_sanitize)

    async def capture_and_respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        captured["info"] = info
        captured["messages"] = messages
        system_texts = [
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        ]
        captured["system_prompt"] = "\n".join(system_texts)
        return ModelResponse(
            parts=[
                TextPart(
                    content="[name] works at [org] and tweets [handle], see [url]."
                )
            ]
        )

    monkeypatch.setattr(
        "thenetwork.settings.get_settings",
        lambda: SimpleNamespace(
            small_agent_model=FunctionModel(capture_and_respond),
            small_agent_api_key="unused-for-concrete-model",
            model_request_timeout_seconds=90.0,
        ),
    )

    result = await sanitize_mod.sanitize_memory_llm(memory, session)

    assert result == "[name] works at [org] and tweets [handle], see [url]."
    assert memory.gist == result
    info = captured["info"]
    assert info.function_tools == []
    assert info.output_tools == []
    prompt = captured["system_prompt"]
    assert "employers" in prompt
    assert "social media handles" in prompt
    assert "URLs" in prompt
    assert "quasi-identifying combinations" in prompt
    assert "Erin Cole" not in result
    assert "Globex" not in result
    assert "@erincodes" not in result
    assert "erin.dev" not in result
    messages = captured["messages"]
    assert "Erin Cole" not in repr(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        "ModelResponse(parts=[TextPart(content='sealed gist')])",
        "You are a PII sanitizer. Return only the sanitized text.",
        "x" * (sanitize_mod.MAX_SANITIZED_GIST_CHARS + 1),
        "   ",
    ],
)
async def test_high_fidelity_sanitizer_downgrades_malformed_model_output(
    monkeypatch,
    malformed: str,
):
    raw = "Alice can help with compiler performance at alice@example.com."
    deterministic = "[name] can help with compiler performance at [email]."
    seen_by_model: list[str] = []

    def deterministic_sanitize(text: str) -> str:
        if text == raw:
            return deterministic
        return text

    async def malformed_llm(text: str) -> str:
        seen_by_model.append(text)
        return malformed

    _enable_llm_tier(monkeypatch)
    monkeypatch.setattr(sanitize_mod, "sanitize_text", deterministic_sanitize)
    monkeypatch.setattr(sanitize_mod, "sanitize_text_llm", malformed_llm)
    audit = MagicMock()
    monkeypatch.setattr(sanitize_mod, "audit_event", audit)

    result = await sanitize_mod.sanitize_text_high_fidelity(raw)

    assert seen_by_model == [deterministic]
    assert result == deterministic
    assert malformed not in result
    audit.assert_called_once_with(
        "sanitize.tier_downgrade", error_type="_UnsafeSanitizerOutput"
    )


@pytest.mark.asyncio
async def test_malformed_sanitizer_output_never_reaches_memory_embedding(
    monkeypatch,
):
    from thenetwork.agent.tools import remember

    raw = "Alice builds compilers; alice@example.com"
    deterministic = "[name] builds compilers; [email]"

    def deterministic_sanitize(text: str) -> str:
        if text == raw:
            return deterministic
        return text

    _enable_llm_tier(monkeypatch)
    monkeypatch.setattr(sanitize_mod, "sanitize_text", deterministic_sanitize)
    monkeypatch.setattr(
        sanitize_mod,
        "sanitize_text_llm",
        AsyncMock(return_value="ModelResponse(parts=[TextPart(content='leak')])"),
    )
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.exec.return_value.one.return_value = 0
    ctx = SimpleNamespace(
        deps=AgentDeps(
            settings=Settings(
                agent_model="test:model",
                small_agent_model="test:model",
                embed_model="test:embed",
                remember_text_max_chars=8_000,
                person_memory_limit=100,
            ),
            sender_email="alice@example.com",
            sender_user_id="user-alice",
            sender_authenticated=True,
            session_factory=lambda: session,
        )
    )
    embedded: list[str] = []

    async def embed(text: str) -> list[float]:
        embedded.append(text)
        return [0.0] * 1536

    monkeypatch.setattr("thenetwork.agent.tools.embed_text", embed)
    monkeypatch.setattr("thenetwork.agent.tools.match_memories", lambda *a, **k: [])

    result = await remember(ctx, text=raw, refs=["user-alice"])

    stored = session.add.call_args_list[0].args[0]
    assert result.get("memory_id") == stored.id
    assert stored.gist == deterministic
    assert embedded == [deterministic]
    assert "ModelResponse" not in repr(stored.gist)
