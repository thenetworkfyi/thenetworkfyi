"""Dependency container injected into every pydantic-ai tool via RunContext."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from thenetwork.settings import Settings, get_settings


@dataclass
class AgentDeps:
    """Passed as deps_type to the pydantic-ai agent."""

    settings: Settings = field(default_factory=get_settings)
    # Injected at runtime; defaults let callers omit in tests
    sender_email: str = ""
    sender_user_id: str | None = None
    # Session factory: () -> contextmanager[Session]
    # Stored as a callable to avoid serialization issues
    session_factory: Callable | None = None
