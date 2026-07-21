"""Shared pytest fixtures: seeded people + memories."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from dotenv import dotenv_values

# The model settings are deliberately required (no defaults - see
# thenetwork/settings.py), and thenetwork.worker.tasks reads Settings at import
# time, so collection needs values from somewhere. Supply test placeholders
# only for keys that neither the environment nor the repo .env already
# provides: in CI (no .env) every placeholder gets set; locally the .env
# values stay authoritative, which tests/scenarios/test_live_archetypes.py
# depends on to reach the real configured model. The .env path is
# cwd-relative to match how pydantic-settings itself resolves env_file.
_dotenv = dotenv_values(Path(".env"))
for _key, _placeholder in (
    ("AGENT_MODEL", "test:model"),
    ("SMALL_AGENT_MODEL", "test:model"),
    ("EMBED_MODEL", "test:embed"),
    ("RELAY_DOMAIN", "relay.example.test"),
):
    if _key not in os.environ and not _dotenv.get(_key):
        os.environ[_key] = _placeholder

from thenetwork.db.models import Person  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://network:network@localhost:5432/test_thenetwork",
)


@pytest.fixture
def seeded_people():
    """In-memory Person objects for unit tests that don't need a DB."""
    return [
        Person(id=str(uuid.uuid4()), name="Alice", email="alice@test.com"),
        Person(id=str(uuid.uuid4()), name="Bob", email="bob@test.com"),
        Person(id=str(uuid.uuid4()), name="Carol", email="carol@test.com"),
        Person(id=str(uuid.uuid4()), name="Dave", email="dave@test.com"),
    ]


@pytest.fixture(scope="session")
def pg_engine():
    """Session-scoped test engine; skips entire session if pgvector DB unreachable."""
    from sqlalchemy import create_engine, text
    from sqlmodel import SQLModel

    try:
        engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        SQLModel.metadata.create_all(engine)
        yield engine
        SQLModel.metadata.drop_all(engine)
        engine.dispose()
    except Exception as exc:
        pytest.skip(f"pgvector DB not reachable ({TEST_DATABASE_URL}): {exc}")


@pytest.fixture
def seeded_db(pg_engine, monkeypatch):
    """Persist people + memories with embeddings to real pgvector DB.

    People: alice, bob, carol, dave.
    Memories (all have gist so they are cross-user eligible):
      alice-mem:  refs=[alice_id], e0            gist="ml engineer"
      bob-mem:    refs=[bob_id],   e1            gist="systems programmer"
      carol-mem:  refs=[carol_id], 0.9*e0+0.1*e1 gist="llm builder"
      intro-mem:  refs=[alice_id, carol_id], e0  gist="connected two ml people"

    query_ml = e0 - nearest memories: alice-mem ≈ intro-mem > carol-mem > bob-mem.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import Session
    import thenetwork.db.session as sess_mod

    def _vec_str(dim0: float = 0.0, dim1: float = 0.0) -> str:
        v = [0.0] * 1536
        v[0] = dim0
        v[1] = dim1
        return "[" + ",".join(str(x) for x in v) + "]"

    def _vec(dim0: float = 0.0, dim1: float = 0.0) -> list[float]:
        v = [0.0] * 1536
        v[0] = dim0
        v[1] = dim1
        return v

    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    carol_id = str(uuid.uuid4())
    dave_id = str(uuid.uuid4())
    mem_alice_id = str(uuid.uuid4())
    mem_bob_id = str(uuid.uuid4())
    mem_carol_id = str(uuid.uuid4())
    mem_intro_id = str(uuid.uuid4())

    test_factory = sessionmaker(
        bind=pg_engine,
        class_=Session,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    monkeypatch.setattr(sess_mod, "_SessionLocal", test_factory)

    test_emails = ["alice@test.com", "bob@test.com", "carol@test.com", "dave@test.com"]

    with pg_engine.connect() as conn:
        conn.execute(
            text("""
            DELETE FROM memories
            WHERE refs && ARRAY(SELECT id FROM people WHERE email = ANY(:e))::text[]
        """),
            {"e": test_emails},
        )
        conn.execute(
            text("DELETE FROM people WHERE email = ANY(:e)"), {"e": test_emails}
        )
        conn.commit()

    with pg_engine.connect() as conn:
        conn.execute(
            text("""
            INSERT INTO people (id, name, email) VALUES
              (:aid, 'Alice', 'alice@test.com'),
              (:bid, 'Bob',   'bob@test.com'),
              (:cid, 'Carol', 'carol@test.com'),
              (:did, 'Dave',  'dave@test.com')
        """),
            {"aid": alice_id, "bid": bob_id, "cid": carol_id, "did": dave_id},
        )
        conn.commit()

    mem_rows = [
        (
            mem_alice_id,
            "Alice is an ML engineer",
            (1.0, 0.0),
            [alice_id],
            "ml engineer",
        ),
        (
            mem_bob_id,
            "Bob writes systems software in Rust",
            (0.0, 1.0),
            [bob_id],
            "systems programmer",
        ),
        (
            mem_carol_id,
            "Carol builds LLM products",
            (0.9, 0.1),
            [carol_id],
            "llm builder",
        ),
        (
            mem_intro_id,
            "Introduced Alice and Carol",
            (1.0, 0.0),
            [alice_id, carol_id],
            "connected two ml people",
        ),
    ]

    with pg_engine.connect() as conn:
        for mem_id, mem_text, emb_dims, refs, gist in mem_rows:
            refs_sql = "ARRAY[" + ",".join(f"'{r}'" for r in refs) + "]::text[]"
            conn.execute(
                text(f"""
                INSERT INTO memories (id, text, embedding, refs, gist, created_at)
                VALUES (:mid, :txt, CAST(:emb AS vector), {refs_sql}, :gist, NOW())
            """),
                {
                    "mid": mem_id,
                    "txt": mem_text,
                    "emb": _vec_str(*emb_dims),
                    "gist": gist,
                },
            )
        conn.commit()

    yield {
        "alice_id": alice_id,
        "bob_id": bob_id,
        "carol_id": carol_id,
        "dave_id": dave_id,
        "mem_alice_id": mem_alice_id,
        "mem_bob_id": mem_bob_id,
        "mem_carol_id": mem_carol_id,
        "mem_intro_id": mem_intro_id,
        "query_ml": _vec(1.0, 0.0),
    }

    with pg_engine.connect() as conn:
        conn.execute(
            text("DELETE FROM memories WHERE id = ANY(:ids)"),
            {"ids": [mem_alice_id, mem_bob_id, mem_carol_id, mem_intro_id]},
        )
        conn.execute(
            text("DELETE FROM people WHERE id = ANY(:ids)"),
            {"ids": [alice_id, bob_id, carol_id, dave_id]},
        )
        conn.commit()
