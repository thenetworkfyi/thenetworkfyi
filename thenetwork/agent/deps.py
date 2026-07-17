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
    # Synthetic jobs emitted by proactive scans are agent prompts, not inbound
    # user messages. A no-op is an expected, auditable outcome for these runs.
    is_proactive: bool = False
    # For proactive runs only: the opaque person id the scan surfaced as the
    # counterpart for sender_user_id. propose_introduction must reject any
    # other_person_id that doesn't match this when is_proactive is set.
    proactive_candidate_id: str | None = None
    # For proactive event runs only: the one opaque event id selected by the
    # server-side scan. The event-send capability rejects every other id.
    proactive_event_id: str | None = None
    # Session factory: () -> contextmanager[Session]
    # Stored as a callable to avoid serialization issues
    session_factory: Callable | None = None
    outbound_send_count: int = 0
    server_side_send_count: int = 0
    introduction_proposal_count: int = 0
