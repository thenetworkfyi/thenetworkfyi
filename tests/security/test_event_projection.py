"""Security contracts for the event projection and capability surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from thenetwork.agent.deps import AgentCapabilities, AgentDeps
from thenetwork.agent.tools import create_event, search_events
from thenetwork.search.events import EventMatch, match_events


def test_event_match_sql_never_selects_raw_text_or_submitter_identity():
    expires_at = datetime.now(timezone.utc) + timedelta(days=3)
    row = SimpleNamespace(
        event_id="opaque-event",
        gist="sealed event gist",
        recurrence="Alice every Friday at alice@example.com",
        expires_at=expires_at,
        similarity=0.91,
    )
    session = MagicMock()
    session.exec.return_value.fetchall.return_value = [row]

    matches = match_events([0.0, 1.0], session, limit=4)

    statement = str(session.exec.call_args.args[0]).lower()
    assert "e.text" not in statement
    assert "e.recurrence" not in statement
    assert "submitter_id" not in statement
    assert "cancelled_at is null" in statement
    assert "expires_at > now()" in statement
    assert matches == [
        EventMatch("opaque-event", "sealed event gist", expires_at, 0.91)
    ]
    assert not hasattr(matches[0], "text")
    assert not hasattr(matches[0], "submitter_id")
    assert not hasattr(matches[0], "recurrence")
    assert session.exec.call_args.kwargs["params"]["limit"] == 4
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_search_events_returns_only_opaque_id_gist_and_lifecycle_fields():
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    match = EventMatch(
        event_id="opaque-event",
        gist="sealed gist",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        similarity=0.8,
    )
    ctx = SimpleNamespace(
        deps=AgentDeps(
            capabilities=AgentCapabilities(
                embed_text=AsyncMock(return_value=[0.0] * 1536),
                match_events=MagicMock(return_value=[match]),
            ),
            sender_user_id="person-1",
            sender_authenticated=True,
            session_factory=lambda: session,
        )
    )

    result = await search_events(ctx, "compiler meetup")

    assert set(result[0]) == {
        "event_id",
        "gist",
        "expires_at",
        "similarity",
    }
    assert "submitter" not in repr(result).lower()
    assert "text" not in result[0]


@pytest.mark.asyncio
async def test_event_create_gist_path_never_returns_raw_cross_user_content():
    raw = "Alice Chen hosts this; alice.chen@example.com; +1 415 555 0100"
    gist = "[name] hosts this; [email]; [phone]"
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    sanitize = MagicMock(return_value=gist)
    ctx = SimpleNamespace(
        deps=AgentDeps(
            capabilities=AgentCapabilities(
                sanitize_text=sanitize,
                embed_text=AsyncMock(return_value=[0.0] * 1536),
            ),
            sender_user_id="person-owner",
            sender_authenticated=True,
            session_factory=lambda: session,
        )
    )

    result = await create_event(
        ctx,
        text=raw,
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )

    sanitize.assert_called_once_with(raw)
    assert result["gist"] == gist
    assert raw not in repr(result)
    assert "submitter_id" not in result
