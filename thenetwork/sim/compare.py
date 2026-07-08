"""Compare two recorded simulation runs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunMetrics:
    introductions: int
    judge_score: float | None
    token_usage: int
    cost_usd: float
    process_email_calls: int


@dataclass(frozen=True)
class MetricDelta:
    name: str
    before: str
    after: str
    delta: str


def compare_runs(before: Path, after: Path) -> tuple[MetricDelta, ...]:
    before_metrics = load_run_metrics(before)
    after_metrics = load_run_metrics(after)
    return (
        _int_delta("introductions", before_metrics.introductions, after_metrics.introductions),
        _score_delta("judge_score", before_metrics.judge_score, after_metrics.judge_score),
        _int_delta("token_usage", before_metrics.token_usage, after_metrics.token_usage),
        _float_delta("cost_usd", before_metrics.cost_usd, after_metrics.cost_usd),
        _int_delta(
            "process_email_calls",
            before_metrics.process_email_calls,
            after_metrics.process_email_calls,
        ),
    )


def load_run_metrics(run_dir: Path) -> RunMetrics:
    events = _read_events(run_dir / "events.jsonl")
    judge_scores = [
        float(event["score"])
        for event in events
        if _is_judge_event(event) and event.get("score") is not None
    ]
    return RunMetrics(
        introductions=sum(1 for event in events if _is_intro_event(event)),
        judge_score=(sum(judge_scores) / len(judge_scores) if judge_scores else None),
        token_usage=sum(
            int(event.get("total_tokens") or event.get("token_usage") or 0)
            for event in events
        ),
        cost_usd=sum(float(event.get("cost_usd") or 0.0) for event in events),
        process_email_calls=sum(
            1 for event in events if event.get("event") == "sim.process_email_started"
        ),
    )


def render_compare(deltas: tuple[MetricDelta, ...]) -> str:
    lines = [
        "| Metric | Before | After | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for delta in deltas:
        lines.append(f"| {delta.name} | {delta.before} | {delta.after} | {delta.delta} |")
    return "\n".join(lines) + "\n"


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _is_intro_event(event: dict[str, Any]) -> bool:
    name = str(event.get("event", ""))
    return (
        "introduction" in name
        or "dispatch_email" in name
        or event.get("tool_name") == "dispatch_email"
    )


def _is_judge_event(event: dict[str, Any]) -> bool:
    name = str(event.get("event", ""))
    return name.startswith("sim.judge") or name.endswith(".judge")


def _int_delta(name: str, before: int, after: int) -> MetricDelta:
    return MetricDelta(name, str(before), str(after), f"{after - before:+d}")


def _float_delta(name: str, before: float, after: float) -> MetricDelta:
    return MetricDelta(name, f"{before:.4f}", f"{after:.4f}", f"{after - before:+.4f}")


def _score_delta(name: str, before: float | None, after: float | None) -> MetricDelta:
    before_text = "n/a" if before is None else f"{before:.2f}"
    after_text = "n/a" if after is None else f"{after:.2f}"
    delta = "n/a" if before is None or after is None else f"{after - before:+.2f}"
    return MetricDelta(name, before_text, after_text, delta)

