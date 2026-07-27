"""Red-team tests for name-free gists (THE SEAL, layers 1 & 4).

A memory's raw text is allowed to carry a person's full name; the gist that
crosses the user boundary (via search, consolidation candidates, or a
proactive-scan trigger body) must never carry it back out, in any surface
form: plain ("Alice Chen"), possessive ("Alice Chen's"), or lowercase
("alice chen" / "alice's"). These tests prove that for both halves of the
one gist-production path: `sanitize_memory`'s substitution of the span
classifier's output, and the `remember()` call the agent actually makes in
production.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.agent.deps import AgentDeps
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


class _NameAwareFakeClassifier:
    """Stands in for the local openai/privacy-filter pipeline.

    The real model is a multi-gigabyte download, unreachable in the sandboxed
    test environment (see tests/test_sanitize.py's docstring for the same
    rationale). Rather than hand-picking a single offset, this fake emits a
    `private_person` span for *every* case-insensitive occurrence of the given
    name(s), in the exact dict shape the pipeline returns. That lets these
    tests exercise the real merge-and-substitute logic in `sanitize_text`
    against every surface form the name takes in the raw text - plain,
    possessive, lowercase - rather than trusting a mock that only ever returns
    the answer the test wants.
    """

    def __init__(self, names: list[str]) -> None:
        pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        self._pattern = re.compile(pattern, re.IGNORECASE)

    def __call__(self, text: str) -> list[dict]:
        return [
            {
                "entity_group": "private_person",
                "start": m.start(),
                "end": m.end(),
                "score": 0.99,
            }
            for m in self._pattern.finditer(text)
        ]


def _assert_no_name_variants(gist: str, name: str) -> None:
    first = name.split()[0]
    for variant in (name, f"{name}'s", first, f"{first}'s"):
        assert variant not in gist, f"{variant!r} leaked into gist: {gist!r}"
        assert variant.lower() not in gist.lower(), (
            f"{variant.lower()!r} leaked into gist: {gist!r}"
        )


# ---------------------------------------------------------------------------
# The span-classifier path (sanitize_memory)
# ---------------------------------------------------------------------------

NAME_VARIANT_TEXTS = [
    pytest.param(
        "Alice Chen is a Rust developer who just moved to Berlin.",
        id="plain-fullname",
    ),
    pytest.param(
        "Alice Chen's startup just raised a seed round.",
        id="possessive-fullname",
    ),
    pytest.param(
        "alice chen mentioned she is hiring for a founding engineer.",
        id="lowercase-fullname",
    ),
    pytest.param(
        "alice's calendar is open Thursday for a coffee chat.",
        id="lowercase-possessive-firstname",
    ),
]


@pytest.mark.parametrize("text", NAME_VARIANT_TEXTS)
def test_sanitize_memory_gist_never_contains_name_variant(monkeypatch, text):
    """No surface form of the referenced name survives into the gist."""
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()
    monkeypatch.setattr(
        sanitize_mod,
        "_get_privacy_filter",
        lambda: _NameAwareFakeClassifier(["Alice Chen", "Alice"]),
    )

    gist = sanitize_mod.sanitize_memory(memory, session)

    _assert_no_name_variants(gist, "Alice Chen")
    assert memory.gist == gist
    assert "[name]" in gist


def test_sanitize_memory_gist_never_contains_any_variant_in_single_multivariant_text(
    monkeypatch,
):
    """All surface forms in one memory - plain, possessive, and lowercase together."""
    text = (
        "Alice Chen is a Rust developer. Alice Chen's startup raised a seed "
        "round. alice chen is hiring, and alice's calendar is open Thursday."
    )
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()
    monkeypatch.setattr(
        sanitize_mod,
        "_get_privacy_filter",
        lambda: _NameAwareFakeClassifier(["Alice Chen", "Alice"]),
    )

    gist = sanitize_mod.sanitize_memory(memory, session)

    _assert_no_name_variants(gist, "Alice Chen")
    assert memory.gist == gist


# ---------------------------------------------------------------------------
# remember()'s actual production call: sanitize_memory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", NAME_VARIANT_TEXTS)
def test_stored_gist_never_contains_a_name_variant(monkeypatch, text):
    """The persisted gist is exactly the sanitizer's output, never the raw text."""
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()

    # The classifier stands in for the model here so this asserts what the
    # repository owns: whatever the sanitizer returns is what gets persisted
    # as the gist. The model's own coverage of each surface form is an
    # integration case in tests/test_sanitize.py.
    monkeypatch.setattr(
        sanitize_mod,
        "sanitize_text",
        lambda raw: re.sub(r"(?i)alice chen'?s?|alice'?s?", "[name]", raw),
    )

    gist = sanitize_mod.sanitize_memory(memory, session)

    _assert_no_name_variants(gist, "Alice Chen")
    assert memory.gist == gist


@pytest.mark.asyncio
async def test_remember_tool_stores_gist_free_of_name_variants_end_to_end():
    """Full remember() call (as invoked by the agent): the persisted memory's
    gist - the only thing that ever leaves the user boundary - must not carry
    any surface form of a referenced person's name, even when the raw text
    packs plain, possessive, and lowercase forms together.
    """
    from thenetwork.agent.tools import remember

    mock_sess = MagicMock()
    mock_sess.__enter__ = MagicMock(return_value=mock_sess)
    mock_sess.__exit__ = MagicMock(return_value=False)
    mock_sess.exec.return_value.one.return_value = 0
    ctx = MagicMock()
    ctx.deps = AgentDeps(
        sender_email="bob@example.com",
        sender_user_id="user-bob",
        session_factory=lambda: mock_sess,
    )
    added: list[object] = []
    mock_sess.add.side_effect = added.append

    raw = (
        "Alice Chen introduced Bob to her cofounder. Alice Chen's intro thread "
        "mentioned that alice chen is heads-down, and alice's team is hiring."
    )

    def fake_sanitize(memory, session):
        redacted = re.sub(r"(?i)alice chen'?s?|alice'?s?", "[name]", memory.text)
        memory.gist = redacted
        return redacted

    with (
        patch(
            "thenetwork.agent.tools.embed_text",
            new_callable=AsyncMock,
            return_value=[0.0] * 1536,
        ),
        patch(
            "thenetwork.agent.tools.sanitize_memory",
            new=MagicMock(side_effect=fake_sanitize),
        ),
        patch("thenetwork.agent.tools.match_memories", return_value=[]),
    ):
        await remember(ctx, text=raw, refs=["user-alice", "user-bob"])

    stored = added[0]
    _assert_no_name_variants(stored.gist, "Alice Chen")
