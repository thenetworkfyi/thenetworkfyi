"""Structural contracts for the test suite's live-model request gate."""

from unittest.mock import patch

import pydantic_ai.models as pydantic_ai_models
import pytest
from pydantic_ai import Agent


@pytest.mark.asyncio
async def test_unmarked_provider_request_is_blocked_before_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-must-never-reach-network")
    agent = Agent("openai:gpt-4o-mini")

    with (
        patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("provider request reached DNS"),
        ),
        pytest.raises(
            RuntimeError,
            match="Model requests are not allowed, since ALLOW_MODEL_REQUESTS is False",
        ),
    ):
        await agent.run("This request must be rejected locally.")


@pytest.mark.integration
@pytest.mark.live_model
def test_live_model_marker_opens_the_request_gate():
    assert pydantic_ai_models.ALLOW_MODEL_REQUESTS is True
