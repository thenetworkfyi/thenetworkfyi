"""Dependency container injected into every pydantic-ai tool via RunContext."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_text
from thenetwork.email.outbound import notify_admins, send_event_fyi, send_reply
from thenetwork.introductions import propose_pair
from thenetwork.memory.sanitize import sanitize_memory, sanitize_text
from thenetwork.memory.sent_email import (
    record_sent_email_memories,
    record_sent_email_memory,
)
from thenetwork.search.events import match_events
from thenetwork.search.match import load_person_evidence, match_memories
from thenetwork.settings import Settings, get_settings


def _legacy_tool_override(name: str, production: Callable) -> Callable:
    """Route old test patches through the capability port during migration."""

    def call(*args, **kwargs):
        tools_module = sys.modules.get("thenetwork.agent.tools")
        override = getattr(tools_module, name, None) if tools_module else None
        return (override or production)(*args, **kwargs)

    return call


@dataclass
class AgentCapabilities:
    """Server-owned infrastructure ports available to agent tools.

    These callables never become model arguments. They preserve the SEAL's
    opaque-id boundaries while making every external effect replaceable as a
    single dependency bundle in tests and simulations.
    """

    default_session_factory: Callable = _legacy_tool_override(
        "get_session", get_session
    )
    embed_text: Callable = _legacy_tool_override("embed_text", embed_text)
    send_reply: Callable = _legacy_tool_override("send_reply", send_reply)
    send_event_fyi: Callable = _legacy_tool_override("send_event_fyi", send_event_fyi)
    notify_admins: Callable = _legacy_tool_override("notify_admins", notify_admins)
    sanitize_memory: Callable = _legacy_tool_override(
        "sanitize_memory", sanitize_memory
    )
    sanitize_text: Callable = _legacy_tool_override("sanitize_text", sanitize_text)
    record_sent_email_memory: Callable = _legacy_tool_override(
        "record_sent_email_memory", record_sent_email_memory
    )
    record_sent_email_memories: Callable = _legacy_tool_override(
        "record_sent_email_memories", record_sent_email_memories
    )
    propose_pair: Callable = _legacy_tool_override("propose_pair", propose_pair)
    match_memories: Callable = _legacy_tool_override("match_memories", match_memories)
    match_events: Callable = _legacy_tool_override("match_events", match_events)
    load_person_evidence: Callable = _legacy_tool_override(
        "load_person_evidence", load_person_evidence
    )
    check_daily_dispatch_cap: Callable | None = None
    consume_daily_dispatch_cap: Callable | None = None


@dataclass
class AgentDeps:
    """Passed as deps_type to the pydantic-ai agent."""

    settings: Settings = field(default_factory=get_settings)
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
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
    # An authenticated sender without a Person record may receive either one
    # fixed welcome or one model-written direct reply. Keep that choice
    # mutually exclusive even when the model calls more than one tool.
    unknown_sender_response_sent: bool = False
    # Set only when a non-email terminal capability (currently escalation)
    # completed its server-owned side effect. Output validation uses this
    # together with server_side_send_count to reject bare final text.
    terminal_action_taken: bool = False
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
