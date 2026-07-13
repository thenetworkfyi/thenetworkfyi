"""Tick loop for simulation harness runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from thenetwork.settings import get_settings
from thenetwork.sim.personas.consent import intro_token, make_reply_thread_faithful
from thenetwork.sim.run.mail import (
    ProcessEmailCallable,
    SimMessageObserver,
    SimPostOffice,
    _extract_body,
    capture_outbound,
    deliver_inbound,
)
from thenetwork.sim.personas.persona import TinyPersonEmailAdapter
from thenetwork.sim.personas.population import SimSchedule
from thenetwork.worker import proactive


ScanCallable = Callable[[int], Awaitable[None]]
ProgressCallable = Callable[[str], None]
ProactiveTriggerObserver = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class TickResult:
    tick: int
    persona_messages: int
    proactive_jobs: int


@dataclass(frozen=True)
class SimLoopResult:
    ticks: tuple[TickResult, ...]
    post_office: SimPostOffice

    @property
    def persona_messages(self) -> int:
        return sum(tick.persona_messages for tick in self.ticks)

    @property
    def proactive_jobs(self) -> int:
        return sum(tick.proactive_jobs for tick in self.ticks)


class SimTickLoop:
    """Drive persona email turns and proactive scans over discrete ticks."""

    def __init__(
        self,
        adapters: Sequence[TinyPersonEmailAdapter],
        *,
        run_dir: Path,
        process: ProcessEmailCallable | None = None,
        proactive_every: int | None = 1,
        rate_limit_per_hour: int = 10_000,
        schedule: SimSchedule | None = None,
        progress: ProgressCallable | None = None,
        on_delivery: SimMessageObserver | None = None,
        on_proactive_trigger: ProactiveTriggerObserver | None = None,
        mbox_path: Path | None = None,
    ) -> None:
        if proactive_every is not None and proactive_every < 1:
            raise ValueError("proactive_every must be at least 1")
        self.adapters = tuple(adapters)
        self.run_dir = run_dir
        self.process = process
        self.proactive_every = proactive_every
        self.rate_limit_per_hour = rate_limit_per_hour
        self.schedule = schedule or SimSchedule()
        self.progress = progress
        self.on_proactive_trigger = on_proactive_trigger
        self.post_office = SimPostOffice(
            mbox_path=mbox_path or run_dir / "all-mail.mbox", on_deliver=on_delivery
        )

    async def run(self, *, ticks: int) -> SimLoopResult:
        if ticks < 1:
            raise ValueError("ticks must be at least 1")
        self.run_dir.mkdir(parents=True, exist_ok=True)

        results: list[TickResult] = []
        with (
            override_rate_limits(self.rate_limit_per_hour),
            capture_outbound(self.post_office),
        ):
            for tick in range(1, ticks + 1):
                self._report(f"tick {tick}/{ticks}: started")
                persona_messages = await self._run_persona_turns(
                    tick, total_ticks=ticks
                )
                proactive_jobs = 0
                if (
                    self.proactive_every is not None
                    and tick % self.proactive_every == 0
                ):
                    proactive_jobs = await run_proactive_scans(
                        timestamp=tick,
                        process=self.process,
                        on_defer=self.on_proactive_trigger,
                    )
                results.append(
                    TickResult(
                        tick=tick,
                        persona_messages=persona_messages,
                        proactive_jobs=proactive_jobs,
                    )
                )
                self._report(
                    f"tick {tick}/{ticks}: completed "
                    f"({persona_messages} persona messages, {proactive_jobs} proactive jobs)"
                )

        return SimLoopResult(ticks=tuple(results), post_office=self.post_office)

    async def _run_persona_turns(self, tick: int, *, total_ticks: int) -> int:
        sent = 0
        for adapter in self.adapters:
            if self.schedule.is_interrupted(adapter.config, tick):
                continue
            replies = self.post_office.pop_all(adapter.config.email)
            consent_threads = [
                reply for reply in replies if intro_token(reply) is not None
            ]
            plain_replies = [reply for reply in replies if intro_token(reply) is None]
            active_thread = consent_threads[0] if consent_threads else None
            if len(consent_threads) > 1:
                # One consent thread per turn: hold the rest so each decision
                # is authored against its own thread and token on a later turn.
                self.post_office.requeue(adapter.config.email, consent_threads[1:])
            turn_replies = (
                [*plain_replies, active_thread]
                if active_thread is not None
                else list(replies)
            )
            reply_texts = tuple(
                text
                for text in (_extract_body(reply).strip() for reply in turn_replies)
                if text
            )
            reply_to = (
                active_thread
                if active_thread is not None
                else (replies[-1] if replies else None)
            )
            thread_token = intro_token(reply_to) if reply_to is not None else None
            events = self.schedule.events_for(adapter.config, tick)
            msg = await adapter.anext_email(
                _tick_prompt(adapter.config.goal, tick, events, reply_texts),
                tick=tick,
                subject=f"Simulation tick {tick}",
                reply_to=reply_to,
                body_filter=lambda body, token=thread_token: make_reply_thread_faithful(
                    body, token
                ),
            )
            if msg is None:
                continue
            prefix = f"tick {tick}/{total_ticks}: {adapter.config.name}: process_email"
            self._report(f"{prefix} started")
            await deliver_inbound(
                msg,
                process=self.process,
                post_office=self.post_office,
                tick=tick,
                persona=adapter.config.name,
            )
            self._report(f"{prefix} completed")
            sent += 1
        return sent

    def _report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


async def run_proactive_scans(
    *,
    timestamp: int,
    process: ProcessEmailCallable | None = None,
    scans: Sequence[Any] | None = None,
    on_defer: ProactiveTriggerObserver | None = None,
) -> int:
    """Run proactive scans and execute their deferred jobs in-loop."""
    captured: list[dict[str, Any]] = []

    def capture_defer(**kwargs: Any) -> None:
        captured.append(kwargs)
        if on_defer is not None:
            on_defer(kwargs)

    scan_tasks = scans or (
        proactive.scan_for_opportunities,
        proactive.scan_for_matches,
    )
    with patch.object(proactive.process_email, "defer", side_effect=capture_defer):
        for scan in scan_tasks:
            await _call_scan(scan, timestamp)

    process_func = process or proactive.process_email.func
    for job in captured:
        await process_func(**job)
    return len(captured)


@contextmanager
def override_rate_limits(rate_limit_per_hour: int):
    """Temporarily relax inbound rate limits for dense simulation ticks."""
    settings = get_settings()
    old_rate = settings.rate_limit_per_hour
    old_unauthenticated = settings.unauthenticated_rate_limit_per_hour
    old_global = settings.global_email_rate_limit_per_hour
    settings.rate_limit_per_hour = rate_limit_per_hour
    settings.unauthenticated_rate_limit_per_hour = rate_limit_per_hour
    settings.global_email_rate_limit_per_hour = max(old_global, rate_limit_per_hour)
    try:
        yield
    finally:
        settings.rate_limit_per_hour = old_rate
        settings.unauthenticated_rate_limit_per_hour = old_unauthenticated
        settings.global_email_rate_limit_per_hour = old_global


async def _call_scan(scan: Any, timestamp: int) -> None:
    target = getattr(scan, "func", scan)
    await target(timestamp)


def _tick_prompt(goal: str, tick: int, events=(), replies: tuple[str, ...] = ()) -> str:
    event_text = " ".join(f"Event: {event.text}" for event in events)
    reply_text = " ".join(f"You received a reply: {reply}" for reply in replies)
    return (
        f"Tick {tick}. Write at most one concise email to The Network if your "
        f"goal still needs action: {goal} {event_text} {reply_text}"
    ).strip()
