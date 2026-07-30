"""CrewAI Flow orchestrator with proactive discovery and observer support for simulation tick loop runs."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path
from time import perf_counter
from typing import Any

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from thenetwork.settings import get_settings
from thenetwork.sim.personas.consent import make_reply_thread_faithful, thread_token_of
from thenetwork.sim.personas.llm_persona import TransientPersonaError
from thenetwork.sim.personas.persona import TinyPersonEmailAdapter
from thenetwork.sim.personas.population import SimSchedule
from thenetwork.sim.run.loop import (
    DrainJobsCallable,
    ProactiveTriggerObserver,
    ProcessEmailCallable,
    ProgressCallable,
    SimLoopResult,
    SimMessageObserver,
    StageTimingObserver,
    TickResult,
    _record_stage_timing,
    _tick_prompt,
    override_rate_limits,
    run_proactive_scans,
)
from thenetwork.sim.run.mail import (
    SimMessageMeta,
    SimPostOffice,
    _extract_body,
    capture_outbound,
    deliver_inbound,
)


class NetworkSimulationState(BaseModel):
    total_ticks: int = 1
    current_tick: int = 0
    completed_ticks: list[dict[str, Any]] = []


class NetworkSimulationFlow(Flow[NetworkSimulationState]):
    """Flow orchestrator for simulation tick loops."""

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
        super().__init__(suppress_flow_events=True)
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
        self.on_stage_timing = on_stage_timing
        self.drain_jobs = drain_jobs
        self.turn_concurrency = (
            get_settings().worker_concurrency
            if turn_concurrency is None
            else turn_concurrency
        )
        if self.turn_concurrency < 1:
            raise ValueError("turn_concurrency must be at least 1")
        self.post_office = SimPostOffice(
            mbox_path=mbox_path or run_dir / "all-mail.mbox", on_deliver=on_delivery
        )

    @start()
    def initialize_flow(self) -> str:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return "initialized"

    @listen(initialize_flow)
    async def execute_simulation(self, _status: str) -> SimLoopResult:
        return await self._run_ticks(ticks=self.state.total_ticks)

    async def _run_ticks(self, *, ticks: int) -> SimLoopResult:
        results: list[TickResult] = []
        with (
            override_rate_limits(self.rate_limit_per_hour),
            capture_outbound(
                self.post_office,
                meta_factory=lambda: SimMessageMeta(
                    tick=self.state.current_tick,
                    direction="agent->persona",
                ),
            ),
        ):
            for tick in range(1, ticks + 1):
                self.state.current_tick = tick
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
                        on_stage_timing=self.on_stage_timing,
                    )
                    await self._drain_jobs()

                tick_res = TickResult(
                    tick=tick,
                    persona_messages=persona_messages,
                    proactive_jobs=proactive_jobs,
                )
                results.append(tick_res)
                self.state.completed_ticks.append(
                    {
                        "tick": tick,
                        "persona_messages": persona_messages,
                        "proactive_jobs": proactive_jobs,
                    }
                )
                self._report(
                    f"tick {tick}/{ticks}: completed "
                    f"({persona_messages} persona messages, {proactive_jobs} proactive jobs)"
                )

        return SimLoopResult(ticks=tuple(results), post_office=self.post_office)

    async def run(self, *, ticks: int) -> SimLoopResult:
        if ticks < 1:
            raise ValueError("ticks must be at least 1")
        result = await self.kickoff_async(inputs={"total_ticks": ticks})
        if not isinstance(result, SimLoopResult):
            raise RuntimeError("simulation flow returned an unexpected result")
        return result

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
        replies = self.post_office.pop_all(adapter.config.email)
        consent_threads = [
            reply for reply in replies if thread_token_of(reply) is not None
        ]
        plain_replies = [reply for reply in replies if thread_token_of(reply) is None]
        active_thread = consent_threads[0] if consent_threads else None
        if len(consent_threads) > 1:
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
                post_office=self.post_office,
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
