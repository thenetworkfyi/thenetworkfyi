"""Shared pytest fixtures including seeded test profiles + graph."""
from __future__ import annotations

import os
import uuid
import pytest

from thenetwork.db.models import Profile, NetworkConnection

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://network:network@localhost:5432/test_thenetwork",
)


def _profile(name: str, email: str, skills: list[str], intent: str) -> Profile:
    return Profile(
        id=str(uuid.uuid4()),
        name=name,
        email=email,
        bio=f"Bio of {name}",
        skills=skills,
        intent_description=intent,
        available_to_collaborate=True,
        identity_vector=[0.1] * 1536,
        intent_vector=[0.2] * 1536,
    )


@pytest.fixture
def seeded_profiles():
    """Multi-user graph fixture. PII present so injection tests can attempt to leak it."""
    alice = _profile("Alice", "alice@example.com", ["python", "ml"], "I want to meet ML engineers")
    bob = _profile("Bob", "bob@example.com", ["rust", "systems"], "Looking for systems programmers")
    carol = _profile("Carol", "carol@example.com", ["python", "ml", "llm"], "Building LLM products")
    dave = _profile("Dave", "dave@example.com", ["product", "growth"], "Seeking co-founders")
    return [alice, bob, carol, dave]


@pytest.fixture
def seeded_connections(seeded_profiles):
    """Directed edges: alice <-> carol (mutual), alice -> dave."""
    alice, _bob, carol, dave = seeded_profiles
    return [
        NetworkConnection(user_id_a=alice.id, user_id_b=carol.id, connection_strength=1.0),
        NetworkConnection(user_id_a=carol.id, user_id_b=alice.id, connection_strength=1.0),
        NetworkConnection(user_id_a=alice.id, user_id_b=dave.id, connection_strength=0.5),
    ]


@pytest.fixture(scope="session")
def pg_engine():
    """Session-scoped test engine; skips entire session if pgvector DB is unreachable."""
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
    """Persist differentiated profiles + connections to real DB; patch session; clean up after.

    Yields a dict with alice_id, bob_id, carol_id, dave_id, and query_ml (a 1536-dim
    vector aligned with alice/carol's intent direction).

    Graph: alice <-> carol (mutual strength 1.0), alice -> dave (0.5).
    Vectors:
      alice  = e_0  (ml direction, used as requester)
      bob    = e_1  (rust direction, dissimilar to query)
      carol  = 0.9*e_0 + 0.1*e_1  (close to alice; has ml skill)
      dave   = e_2  (product direction)
    """
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    import thenetwork.db.session as sess_mod

    def _vec(dim0: float = 0.0, dim1: float = 0.0, dim2: float = 0.0) -> list[float]:
        v = [0.0] * 1536
        v[0] = dim0
        v[1] = dim1
        v[2] = dim2
        return v

    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    carol_id = str(uuid.uuid4())
    dave_id = str(uuid.uuid4())

    test_factory = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    monkeypatch.setattr(sess_mod, "_SessionLocal", test_factory)

    with pg_engine.connect() as conn:
        conn.execute(text("""
            DELETE FROM network_connections
            WHERE user_id_a IN (SELECT id FROM profiles WHERE email = ANY(:emails))
               OR user_id_b IN (SELECT id FROM profiles WHERE email = ANY(:emails))
        """), {"emails": ["alice@test.com", "bob@test.com", "carol@test.com", "dave@test.com"]})
        conn.execute(text(
            "DELETE FROM profiles WHERE email = ANY(:emails)"
        ), {"emails": ["alice@test.com", "bob@test.com", "carol@test.com", "dave@test.com"]})
        conn.commit()

    with pg_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO profiles
              (id, name, email, bio, skills, intent_description, available_to_collaborate, intent_vector)
            VALUES
              (:aid, 'Alice', 'alice@test.com', '', ARRAY['python','ml']::text[],
               'ml engineer', true, CAST(:av AS vector)),
              (:bid, 'Bob', 'bob@test.com', '', ARRAY['rust','systems']::text[],
               'systems', true, CAST(:bv AS vector)),
              (:cid, 'Carol', 'carol@test.com', '', ARRAY['python','ml','llm']::text[],
               'llm builder', true, CAST(:cv AS vector)),
              (:did, 'Dave', 'dave@test.com', '', ARRAY['product']::text[],
               'co-founder', true, CAST(:dv AS vector))
        """), {
            "aid": alice_id, "av": str(_vec(1.0, 0.0, 0.0)),
            "bid": bob_id,   "bv": str(_vec(0.0, 1.0, 0.0)),
            "cid": carol_id, "cv": str(_vec(0.9, 0.1, 0.0)),
            "did": dave_id,  "dv": str(_vec(0.0, 0.0, 1.0)),
        })
        conn.execute(text("""
            INSERT INTO network_connections (user_id_a, user_id_b, connection_strength)
            VALUES
              (:aid, :cid, 1.0),
              (:cid, :aid, 1.0),
              (:aid, :did, 0.5)
        """), {"aid": alice_id, "cid": carol_id, "did": dave_id})
        conn.commit()

    yield {
        "alice_id": alice_id,
        "bob_id":   bob_id,
        "carol_id": carol_id,
        "dave_id":  dave_id,
        "query_ml": _vec(1.0, 0.0, 0.0),
    }

    with pg_engine.connect() as conn:
        conn.execute(
            text("DELETE FROM network_connections WHERE user_id_a = ANY(:ids) OR user_id_b = ANY(:ids)"),
            {"ids": [alice_id, bob_id, carol_id, dave_id]},
        )
        conn.execute(
            text("DELETE FROM profiles WHERE id = ANY(:ids)"),
            {"ids": [alice_id, bob_id, carol_id, dave_id]},
        )
        conn.commit()
