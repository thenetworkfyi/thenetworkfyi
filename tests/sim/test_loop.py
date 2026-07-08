from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from thenetwork.settings import get_settings
from thenetwork.sim.loop import SimTickLoop, override_rate_limits, run_proactive_scans
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.worker import proactive


class ScriptedTinyPerson:
    def __init__(self, replies: list[str]) -> None:
        self.name = "Scripted"
        self.replies = replies

    def listen_and_act(self, _stimulus: str):
        return {"content": self.replies.pop(0)}


def _adapter(name: str, email: str, replies: list[str], budget: int = 2):
    return TinyPersonEmailAdapter(
        ScriptedTinyPerson(replies),
        PersonaConfig(
            name=name,
            email=email,
            goal="Find a strong match.",
            stop_condition="Stop when registered.",
            message_budget=budget,
            agent_address="join@example.test",
        ),
    )


@pytest.mark.asyncio
async def test_tick_loop_advances_time_and_processes_persona_messages(tmp_path):
    process = AsyncMock()
    loop = SimTickLoop(
        [_adapter("Priya", "priya@example.test", ["one", "two"], budget=2)],
        run_dir=tmp_path,
        process=process,
        proactive_every=10,
    )

    result = await loop.run(ticks=3)

    assert [tick.tick for tick in result.ticks] == [1, 2, 3]
    assert result.persona_messages == 2
    assert process.await_count == 2
    assert len(result.post_office.messages_for("join@example.test")) == 2


@pytest.mark.asyncio
async def test_proactive_scan_defers_are_executed_in_loop():
    async def fake_scan(_timestamp: int) -> None:
        proactive.process_email.defer(
            sender_email="priya@example.test",
            subject="[Proactive] Possible connection",
            body="opaque ids and gists only",
        )

    process = AsyncMock()

    count = await run_proactive_scans(timestamp=3, process=process, scans=(fake_scan,))

    assert count == 1
    process.assert_awaited_once_with(
        sender_email="priya@example.test",
        subject="[Proactive] Possible connection",
        body="opaque ids and gists only",
    )


def test_override_rate_limits_restores_settings():
    settings = get_settings()
    old = (
        settings.rate_limit_per_hour,
        settings.unauthenticated_rate_limit_per_hour,
        settings.global_email_rate_limit_per_hour,
    )

    with override_rate_limits(1234):
        assert settings.rate_limit_per_hour == 1234
        assert settings.unauthenticated_rate_limit_per_hour == 1234
        assert settings.global_email_rate_limit_per_hour >= 1234

    assert (
        settings.rate_limit_per_hour,
        settings.unauthenticated_rate_limit_per_hour,
        settings.global_email_rate_limit_per_hour,
    ) == old

