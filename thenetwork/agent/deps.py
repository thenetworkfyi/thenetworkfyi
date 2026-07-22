"""Dependency container injected into every pydantic-ai tool via RunContext."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from thenetwork.settings import Settings, get_settings


@dataclass
class AgentDeps:
    """Passed as deps_type to the pydantic-ai agent."""

    settings: Settings = field(default_factory=get_settings)
    # Injected at runtime; defaults let callers omit in tests
    sender_email: str = ""
    sender_user_id: str | None = None
    inbound_subject: str = ""
    # The current untrusted message body. Tools may inspect it only to enforce
    # server-side policy; it is never an identity or recipient authority.
    inbound_body: str = ""
    inbound_message_id: str | None = None
    inbound_references: str | None = None
    inbound_body_for_quote: str | None = None
    inbound_date: str | None = None
    trace_id: str | None = None
    # True only when the third-party IMAP provider's Authentication-Results
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
    # The exact event version whose sealed gist appeared in the trigger. The
    # event-send capability rejects stale jobs after an owner edits the event.
    proactive_event_version: int | None = None
    # Session factory: () -> contextmanager[Session]
    # Stored as a callable to avoid serialization issues
    session_factory: Callable | None = None
    outbound_send_count: int = 0
    server_side_send_count: int = 0
    introduction_proposal_count: int = 0
    # Server-owned replay state for mutating tools within one model run. Keys
    # are canonical argument fingerprints plus their occurrence in the first
    # generation; raw tool arguments are never retained here or audited.
    mutating_tool_results: dict[tuple[str, int], dict[str, Any]] = field(
        default_factory=dict, repr=False
    )
    mutating_tool_generation_counts: dict[tuple[int, str], int] = field(
        default_factory=dict, repr=False
    )
    mutating_tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
