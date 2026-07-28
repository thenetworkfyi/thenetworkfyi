"""Tick loop for simulation harness runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

from thenetwork.settings import get_settings
from thenetwork.sim.personas.consent import make_reply_thread_faithful, thread_token_of
from thenetwork.sim.personas.llm_persona import TransientPersonaError
from thenetwork.sim.run.mail import (
    ProcessEmailCallable,
    SimMessageObserver,
    SimPostOffice,
    _extract_body,
    deliver_inbound,
)
from thenetwork.sim.personas.persona import TinyPersonEmailAdapter
from thenetwork.sim.personas.population import SimSchedule
from thenetwork.worker import event_scan, proactive


ScanCallable = Callable[[int], Awaitable[None]]
ProgressCallable = Callable[[str], None]
ProactiveTriggerObserver = Callable[[dict[str, Any]], None]
DrainJobsCallable = Callable[[], Awaitable[None]]
StageTimingObserver = Callable[..., None]


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
    """Drive persona email turns and periodic discovery over discrete ticks via NetworkSimulationFlow."""

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
        on_stage_timing: StageTimingObserver | None = None,
        drain_jobs: DrainJobsCallable | None = None,
        turn_concurrency: int | None = None,
        mbox_path: Path | None = None,
    ) -> None:
        from thenetwork.sim.run.crew_flow import NetworkSimulationFlow

        self.flow = NetworkSimulationFlow(
            adapters,
            run_dir=run_dir,
            process=process,
            proactive_every=proactive_every,
            rate_limit_per_hour=rate_limit_per_hour,
            schedule=schedule,
            progress=progress,
            on_delivery=on_delivery,
            on_proactive_trigger=on_proactive_trigger,
            on_stage_timing=on_stage_timing,
            drain_jobs=drain_jobs,
            turn_concurrency=turn_concurrency,
            mbox_path=mbox_path,
        )
        self.post_office = self.flow.post_office

    async def run(self, *, ticks: int) -> SimLoopResult:
        return await self.flow.run(ticks=ticks)

    async def _run_persona_turns(self, tick: int, *, total_ticks: int) -> int:
        semaphore = asyncio.Semaphore(self.turn_concurrency)

        async def generate(adapter: TinyPersonEmailAdapter):
            async with semaphore:
                try:
                    return await self._generate_persona_message(adapter, tick)
                except TransientPersonaError:
                    self._report(
                        f"tick {tick}/{total_ticks}: {adapter.config.name}: "
                        "persona generation skipped after transient provider errors"
                    )
                    return None

        generated = await asyncio.gather(
            *(
                generate(adapter)
                for adapter in self.adapters
                if not self.schedule.is_interrupted(adapter.config, tick)
            )
        )
        pending = tuple(item for item in generated if item is not None)
        prefixes = tuple(
            f"tick {tick}/{total_ticks}: {adapter.config.name}: process_email"
            for adapter, _message in pending
        )
        for prefix in prefixes:
            self._report(f"{prefix} started")

        await asyncio.gather(
            *(
                deliver_inbound(
                    message,
                    process=self.process,
                    post_office=self.post_office,
                    tick=tick,
                    persona=adapter.config.name,
                )
                for adapter, message in pending
            )
        )
        await self._drain_jobs()

        for prefix in prefixes:
            self._report(f"{prefix} completed")
        return len(pending)

    async def _generate_persona_message(
        self, adapter: TinyPersonEmailAdapter, tick: int
    ) -> tuple[TinyPersonEmailAdapter, EmailMessage] | None:
        """Generate one persona's turn from the mailbox snapshot for this tick."""
        replies = self.post_office.pop_all(adapter.config.email)
        consent_threads = [
            reply for reply in replies if thread_token_of(reply) is not None
        ]
        plain_replies = [reply for reply in replies if thread_token_of(reply) is None]
        active_thread = consent_threads[0] if consent_threads else None
        if len(consent_threads) > 1:
            # One consent thread per turn: hold the rest so each decision is
            # authored against its own thread and token on a later turn.
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
        active = thread_token_of(reply_to) if reply_to is not None else None
        thread_kind = active[0] if active is not None else "intro"
        thread_token = active[1] if active is not None else None
        events = self.schedule.events_for(adapter.config, tick)
        started_at = perf_counter()
        timing_fields: dict[str, Any] = {
            "tick": tick,
            "persona": adapter.config.name,
        }
        try:
            message = await adapter.anext_email(
                _tick_prompt(adapter.config.goal, tick, events, reply_texts),
                tick=tick,
                subject=f"Simulation tick {tick}",
                reply_to=reply_to,
                body_filter=lambda body, token=thread_token, kind=thread_kind: (
                    make_reply_thread_faithful(body, token, kind)
                ),
            )
        except BaseException as exc:
            timing_fields.update(
                status="failed",
                error_type=getattr(exc, "error_type", type(exc).__name__),
            )
            raise
        else:
            timing_fields["status"] = "succeeded"
        finally:
            _record_stage_timing(
                self.on_stage_timing,
                "sim.persona_generation_completed",
                started_at,
                **timing_fields,
            )
        if message is None:
            return None
        return adapter, message

    def _report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    async def _drain_jobs(self) -> None:
        if self.drain_jobs is not None:
            await self.drain_jobs()


async def run_proactive_scans(
    *,
    timestamp: int,
    process: ProcessEmailCallable | None = None,
    scans: Sequence[Any] | None = None,
    on_defer: ProactiveTriggerObserver | None = None,
    on_stage_timing: StageTimingObserver | None = None,
) -> int:
    """Run people and event discovery scans and execute deferred jobs in-loop."""
    captured: list[dict[str, Any]] = []

    def capture_defer(**kwargs: Any) -> None:
        captured.append(kwargs)
        if on_defer is not None:
            on_defer(kwargs)

    scan_tasks = scans or (
        proactive.scan_for_opportunities,
        proactive.scan_for_matches,
        event_scan.scan_for_event_recommendations,
    )
    with patch.object(proactive.process_email, "defer", side_effect=capture_defer):
        for scan in scan_tasks:
            await _call_scan(scan, timestamp)

    process_func = process or proactive.process_email.func
    for job in captured:
        started_at = perf_counter()
        timing_fields: dict[str, Any] = {"trace_id": job.get("trace_id")}
        try:
            await process_func(**job)
        except BaseException as exc:
            timing_fields.update(status="failed", error_type=type(exc).__name__)
            raise
        else:
            timing_fields["status"] = "succeeded"
        finally:
            _record_stage_timing(
                on_stage_timing,
                "sim.proactive_job_processed",
                started_at,
                **timing_fields,
            )
    return len(captured)


def _record_stage_timing(
    observer: StageTimingObserver | None,
    event: str,
    started_at: float,
    **fields: Any,
) -> None:
    if observer is not None:
        observer(
            event, elapsed_ms=round((perf_counter() - started_at) * 1000), **fields
        )


# A sim run must never be constrained by the production daily token budget,
# whose default (see settings.py) is sized for real inbound traffic, not a
# dense multi-tick simulation. This floor is a token count, not derived from
# `rate_limit_per_hour` (an email-per-hour figure) - the two aren't
# comparable units - so it is simply large enough that no simulated run
# realistically reaches it.
_SIM_DAILY_TOKEN_CAP_FLOOR = 1_000_000_000


@contextmanager
def override_rate_limits(rate_limit_per_hour: int):
    """Temporarily relax inbound rate limits for dense simulation ticks."""
    settings = get_settings()
    old_rate = settings.rate_limit_per_hour
    old_unauthenticated = settings.unauthenticated_rate_limit_per_hour
    old_global = settings.global_email_rate_limit_per_hour
    old_token_cap = settings.daily_agent_token_cap
    settings.rate_limit_per_hour = rate_limit_per_hour
    settings.unauthenticated_rate_limit_per_hour = rate_limit_per_hour
    settings.global_email_rate_limit_per_hour = max(old_global, rate_limit_per_hour)
    settings.daily_agent_token_cap = max(old_token_cap, _SIM_DAILY_TOKEN_CAP_FLOOR)
    try:
        yield
    finally:
        settings.rate_limit_per_hour = old_rate
        settings.unauthenticated_rate_limit_per_hour = old_unauthenticated
        settings.global_email_rate_limit_per_hour = old_global
        settings.daily_agent_token_cap = old_token_cap


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
