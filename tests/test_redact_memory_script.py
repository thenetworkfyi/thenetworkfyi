from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sql_text

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
            new=AsyncMock(),
        ) as mock_embed,
    ):
        summary = await redact_memory_record("mem-1", session, commit=False)

    assert summary["memory_id"] == "mem-1"
    assert summary["committed"] is False
    assert summary["text_changed"] is True
    assert summary["new_text"] == "[NAME] at [EMAIL] called [PHONE]"
    assert session.rolled_back is True
    assert session.committed is False
    mock_embed.assert_not_called()


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
async def test_redact_memory_ref_gist_refreshed():
    mem = Memory(
        id="mem-ref-1",
        text="John Doe at john@example.com called 555-1234",
        refs=["person-1"],
        gist="Old gist with John Doe",
    )
    session = FakeRedactSession(mem)

    async def fake_sanitize_high_fidelity(memory, sess):
        memory.gist = "[NAME] at [EMAIL] called [PHONE]"
        return memory.gist

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_text",
            return_value="[NAME] at [EMAIL] called [PHONE]",
        ),
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            side_effect=fake_sanitize_high_fidelity,
        ),
        patch(
            "thenetwork.scripts.redact_memory.embed_text",
            new=AsyncMock(return_value=[0.5] * 1536),
        ) as mock_embed,
    ):
        summary = await redact_memory_record("mem-ref-1", session, commit=True)

    assert summary["new_gist"] == "[NAME] at [EMAIL] called [PHONE]"
    assert mem.gist == "[NAME] at [EMAIL] called [PHONE]"
    mock_embed.assert_called_once_with("[NAME] at [EMAIL] called [PHONE]")


@pytest.mark.asyncio
async def test_redact_memory_ref_fails_without_gist():
    mem = Memory(
        id="mem-ref-fail",
        text="Some text with ref",
        refs=["person-1"],
        gist=None,
    )
    session = FakeRedactSession(mem)

    async def fake_sanitize_high_fidelity_none(memory, sess):
        memory.gist = None
        return None

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            side_effect=fake_sanitize_high_fidelity_none,
        ),
        patch(
            "thenetwork.scripts.redact_memory.embed_text",
            new=AsyncMock(),
        ) as mock_embed,
    ):
        with pytest.raises(RuntimeError, match="Sanitization failed"):
            await redact_memory_record("mem-ref-fail", session, commit=True)

    mock_embed.assert_not_called()
    assert session.committed is False


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


def test_build_parser_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["mem-123", "--string", "secret", "--pattern", "s.*"])
    assert exc_info.value.code == 2


def test_cli_main_dry_run_message(capsys):
    mem = Memory(
        id="mem-dry",
        text="John Doe at john@example.com",
        refs=["person-1"],
        gist="John Doe at john@example.com",
    )
    session = FakeRedactSession(mem)

    @contextmanager
    def fake_get_session():
        yield session

    async def fake_sanitize_high_fidelity(memory, sess):
        memory.gist = "[NAME] at [EMAIL]"
        return memory.gist

    with (
        patch(
            "thenetwork.scripts.redact_memory.get_session", side_effect=fake_get_session
        ),
        patch(
            "thenetwork.scripts.redact_memory.sanitize_text",
            return_value="[NAME] at [EMAIL]",
        ),
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            side_effect=fake_sanitize_high_fidelity,
        ),
        patch(
            "thenetwork.scripts.redact_memory.embed_text",
            new=AsyncMock(),
        ) as mock_embed,
    ):
        main(["mem-dry"])

    captured = capsys.readouterr()
    assert (
        "Dry run complete; no changes were committed to database. Embedding is recomputed only on --commit."
        in captured.out
    )
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_redact_memory_dry_run_audits_distinct_outcome():
    mem = Memory(
        id="mem-dry-audit",
        text="John Doe at john@example.com",
        refs=["person-1"],
        gist="John Doe at john@example.com",
    )
    session = FakeRedactSession(mem)

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_text",
            return_value="[NAME] at [EMAIL]",
        ),
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            new=AsyncMock(return_value="[NAME] at [EMAIL]"),
        ),
        patch("thenetwork.scripts.redact_memory.embed_text", new=AsyncMock()),
        patch("thenetwork.scripts.redact_memory.audit_event") as mock_audit,
    ):
        await redact_memory_record("mem-dry-audit", session, commit=False)

    outcomes = [call.kwargs.get("outcome") for call in mock_audit.call_args_list]
    assert "dry_run" in outcomes
    assert "blocked" not in outcomes


@pytest.mark.asyncio
async def test_redact_memory_ref_fails_without_gist_audits_blocked_and_rolls_back():
    mem = Memory(
        id="mem-ref-fail-audit",
        text="Some text with ref",
        refs=["person-1"],
        gist=None,
    )
    session = FakeRedactSession(mem)

    async def fake_sanitize_high_fidelity_none(memory, sess):
        memory.gist = None
        return None

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            side_effect=fake_sanitize_high_fidelity_none,
        ),
        patch("thenetwork.scripts.redact_memory.embed_text", new=AsyncMock()),
        patch("thenetwork.scripts.redact_memory.audit_event") as mock_audit,
    ):
        with pytest.raises(RuntimeError, match="Sanitization failed"):
            await redact_memory_record("mem-ref-fail-audit", session, commit=True)

    assert session.rolled_back is True
    assert session.committed is False
    outcomes = [call.kwargs.get("outcome") for call in mock_audit.call_args_list]
    assert outcomes == ["blocked"]


@pytest.mark.asyncio
async def test_redact_memory_sanitize_exception_audits_blocked_and_rolls_back():
    mem = Memory(
        id="mem-ref-fail-exc",
        text="Some text with ref",
        refs=["person-1"],
        gist="Old gist",
    )
    session = FakeRedactSession(mem)

    with (
        patch(
            "thenetwork.scripts.redact_memory.sanitize_memory_high_fidelity",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("thenetwork.scripts.redact_memory.embed_text", new=AsyncMock()),
        patch("thenetwork.scripts.redact_memory.audit_event") as mock_audit,
    ):
        with pytest.raises(RuntimeError, match="Sanitization failed"):
            await redact_memory_record("mem-ref-fail-exc", session, commit=True)

    assert session.rolled_back is True
    assert session.committed is False
    outcomes = [call.kwargs.get("outcome") for call in mock_audit.call_args_list]
    assert outcomes == ["blocked"]


def test_build_parser_rejects_empty_string():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["mem-123", "--string", ""])
    assert exc_info.value.code == 2


def test_build_parser_rejects_empty_pattern():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["mem-123", "--pattern", ""])
    assert exc_info.value.code == 2


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


def _redact_test_vector(dim0: float = 1.0, dim1: float = 0.0) -> str:
    v = [0.0] * 1536
    v[0] = dim0
    v[1] = dim1
    return "[" + ",".join(str(x) for x in v) + "]"


def _insert_redact_test_memory(
    conn,
    *,
    memory_id: str,
    raw_text: str,
    refs: list[str],
    gist: str | None,
) -> None:
    refs_sql = "ARRAY[" + ",".join(f"'{r}'" for r in refs) + "]::text[]"
    conn.execute(
        sql_text(f"""
        INSERT INTO memories (id, text, embedding, refs, gist, created_at)
        VALUES (:mid, :txt, CAST(:emb AS vector), {refs_sql}, :gist, NOW())
        """),
        {"mid": memory_id, "txt": raw_text, "emb": _redact_test_vector(), "gist": gist},
    )


def _read_redact_test_memory(conn, memory_id: str):
    return conn.execute(
        sql_text("SELECT text, gist, embedding FROM memories WHERE id = :mid"),
        {"mid": memory_id},
    ).one()


def _parse_redact_test_vector(value) -> list[float]:
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",")]
    return list(value)


def _force_deterministic_sanitize_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "thenetwork.settings.get_settings",
        lambda: SimpleNamespace(sanitize_llm_tier_enabled=False),
    )


@pytest.mark.integration
@pytest.mark.real_presidio
def test_redact_memory_dry_run_leaves_persisted_row_unchanged(
    seeded_db, pg_engine, monkeypatch
):
    """A dry run through main() against a real session persists no change."""
    pytest.importorskip("presidio_analyzer")
    memory_id = str(uuid.uuid4())
    raw_text = "Contact Jane Roe at jane.roe@example.com about the merger"
    original_gist = "discussed a business matter"
    with pg_engine.connect() as conn:
        _insert_redact_test_memory(
            conn,
            memory_id=memory_id,
            raw_text=raw_text,
            refs=[seeded_db["alice_id"]],
            gist=original_gist,
        )
        conn.commit()

    with pg_engine.connect() as conn:
        before = _read_redact_test_memory(conn, memory_id)

    _force_deterministic_sanitize_tier(monkeypatch)

    with patch(
        "thenetwork.scripts.redact_memory.embed_text", new=AsyncMock()
    ) as mock_embed:
        main([memory_id])

    mock_embed.assert_not_called()

    with pg_engine.connect() as conn:
        after = _read_redact_test_memory(conn, memory_id)

    assert after.text == before.text
    assert after.gist == before.gist
    assert _parse_redact_test_vector(after.embedding) == _parse_redact_test_vector(
        before.embedding
    )


@pytest.mark.integration
@pytest.mark.real_presidio
def test_redact_memory_commit_persists_gist_regenerated_from_redacted_text(
    seeded_db, pg_engine, monkeypatch
):
    """A committed run persists redacted text, a fresh gist, and a gist embedding."""
    pytest.importorskip("presidio_analyzer")
    memory_id = str(uuid.uuid4())
    raw_text = "Contact Priya Singh at priya.singh@example.com about the deal"
    with pg_engine.connect() as conn:
        _insert_redact_test_memory(
            conn,
            memory_id=memory_id,
            raw_text=raw_text,
            refs=[seeded_db["alice_id"]],
            gist="stale old gist",
        )
        conn.commit()

    _force_deterministic_sanitize_tier(monkeypatch)

    embed_calls: list[str] = []

    async def fake_embed_text(text_arg: str) -> list[float]:
        embed_calls.append(text_arg)
        return [0.42] * 1536

    with patch(
        "thenetwork.scripts.redact_memory.embed_text", side_effect=fake_embed_text
    ):
        main([memory_id, "--commit"])

    with pg_engine.connect() as conn:
        row = _read_redact_test_memory(conn, memory_id)

    assert "Priya Singh" not in row.text
    assert "priya.singh@example.com" not in row.text
    assert row.gist != "stale old gist"
    assert row.gist is not None
    assert embed_calls == [row.gist]
    assert _parse_redact_test_vector(row.embedding)[0] == pytest.approx(0.42)


@pytest.mark.integration
@pytest.mark.real_presidio
def test_redact_memory_dry_run_then_commit_end_to_end(
    seeded_db, pg_engine, monkeypatch
):
    """dry run then --commit on a ref-carrying memory exercises the assembled chain."""
    pytest.importorskip("presidio_analyzer")
    memory_id = str(uuid.uuid4())
    raw_text = "Reach out to Marco Diaz at marco.diaz@example.com re: onboarding"
    with pg_engine.connect() as conn:
        _insert_redact_test_memory(
            conn,
            memory_id=memory_id,
            raw_text=raw_text,
            refs=[seeded_db["alice_id"]],
            gist="original gist",
        )
        conn.commit()

    _force_deterministic_sanitize_tier(monkeypatch)

    with patch(
        "thenetwork.scripts.redact_memory.embed_text", new=AsyncMock()
    ) as mock_embed:
        main([memory_id])
    mock_embed.assert_not_called()

    with pg_engine.connect() as conn:
        after_dry_run = _read_redact_test_memory(conn, memory_id)
    assert after_dry_run.text == raw_text
    assert after_dry_run.gist == "original gist"

    with patch(
        "thenetwork.scripts.redact_memory.embed_text",
        new=AsyncMock(return_value=[0.7] * 1536),
    ) as mock_embed_commit:
        main([memory_id, "--commit"])
    mock_embed_commit.assert_called_once()

    with pg_engine.connect() as conn:
        after_commit = _read_redact_test_memory(conn, memory_id)

    assert "Marco Diaz" not in after_commit.text
    assert "marco.diaz@example.com" not in after_commit.text
    assert after_commit.gist != "original gist"
    assert _parse_redact_test_vector(after_commit.embedding)[0] == pytest.approx(0.7)
