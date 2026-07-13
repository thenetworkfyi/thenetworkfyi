"""Reusable simulation scenarios."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path

from thenetwork.sim.run.mail import (
    SimPostOffice,
    deliver_inbound,
    publish_redacted_mbox,
    render_transcript,
)
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter


@dataclass(frozen=True)
class StrongMatchResult:
    post_office: SimPostOffice
    transcript: str
    mbox_path: Path
    transcript_path: Path
    persona_message_count: int


class StrongMatchScenario:
    """Two-person story meant to exercise a high-confidence introduction."""

    def __init__(
        self,
        adapters: Sequence[TinyPersonEmailAdapter],
        *,
        run_dir: Path,
    ) -> None:
        if len(adapters) != 2:
            raise ValueError("strong-match scenario requires exactly two personas")
        self.adapters = tuple(adapters)
        self.run_dir = run_dir
        self.private_dir = run_dir / "private"
        self.private_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.private_dir, 0o700)
        self.raw_mbox_path = self.private_dir / "all-mail.mbox"
        self.post_office = SimPostOffice(mbox_path=self.raw_mbox_path)

    async def run(self, *, process=None) -> StrongMatchResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        sent = 0
        for tick, adapter in enumerate(self.adapters, start=1):
            msg = adapter.next_email(
                _initial_prompt(adapter.config),
                tick=tick,
                subject="Strong match scenario",
            )
            if msg is None:
                continue
            await deliver_inbound(
                msg,
                process=process,
                post_office=self.post_office,
                tick=tick,
                persona=adapter.config.name,
            )
            sent += 1

        transcript_path = self.run_dir / "transcript.md"
        mbox_path = self.run_dir / "all-mail.mbox"
        publish_redacted_mbox(self.raw_mbox_path, mbox_path)
        transcript = render_transcript(mbox_path, transcript_path)
        return StrongMatchResult(
            post_office=self.post_office,
            transcript=transcript,
            mbox_path=mbox_path,
            transcript_path=transcript_path,
            persona_message_count=sent,
        )


def default_strong_match_configs(
    *,
    agent_address: str = "join@thenetwork.test",
) -> tuple[PersonaConfig, PersonaConfig]:
    return (
        PersonaConfig(
            name="Priya Shah",
            email="priya.sim@example.test",
            goal=(
                "Find someone working on applied ML infrastructure for "
                "manufacturing operations."
            ),
            stop_condition="Stop after you have clearly registered what kind of match you want.",
            message_budget=2,
            agent_address=agent_address,
        ),
        PersonaConfig(
            name="Samir Vale",
            email="samir.sim@example.test",
            goal=(
                "Find operators who need help deploying ML infrastructure in "
                "factory environments."
            ),
            stop_condition="Stop after you have stated the concrete overlap you are seeking.",
            message_budget=2,
            agent_address=agent_address,
        ),
    )


def _initial_prompt(config: PersonaConfig) -> str:
    return (
        "Write one concise email to The Network. "
        f"Your goal: {config.goal} "
        f"Stop condition: {config.stop_condition}"
    )
