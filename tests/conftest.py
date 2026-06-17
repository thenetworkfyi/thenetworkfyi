"""Shared pytest fixtures including seeded test profiles + graph."""
from __future__ import annotations

import uuid
import pytest
from unittest.mock import MagicMock, patch

from thenetwork.db.models import Profile, NetworkConnection


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
    alice, bob, carol, dave = seeded_profiles
    return [
        NetworkConnection(user_id_a=alice.id, user_id_b=carol.id, connection_strength=1.0),
        NetworkConnection(user_id_a=carol.id, user_id_b=alice.id, connection_strength=1.0),
        NetworkConnection(user_id_a=alice.id, user_id_b=dave.id, connection_strength=0.5),
    ]
