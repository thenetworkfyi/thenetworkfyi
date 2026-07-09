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
    inbound_subject: str = ""
    inbound_message_id: str | None = None
    inbound_references: str | None = None
    inbound_body_for_quote: str | None = None
    inbound_date: str | None = None
    trace_id: str | None = None
    # True only when the receiving mail server's Authentication-Results
    # header vouched for this sender's DKIM/SPF (see email/inbound.py).
    # Tools that can create or mutate identity (e.g. register_person) must
    # gate on this - the From: header alone is spoofable.
    sender_authenticated: bool = False
    # Session factory: () -> contextmanager[Session]
    # Stored as a callable to avoid serialization issues
    session_factory: Callable | None = None
    dispatch_email_sent_count: int = 0
    server_side_send_count: int = 0
