"""Tick loop for simulation harness runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

from thenetwork.settings import get_settings
from thenetwork.sim.run.mail import (
    ProcessEmailCallable,
    SimMessageObserver,
    SimPostOffice,
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
