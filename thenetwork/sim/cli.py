"""Command line entrypoint for the simulation harness."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from thenetwork.sim.compare import compare_runs, render_compare
from thenetwork.sim.persona import TinyPersonEmailAdapter
from thenetwork.sim.recorder import SimRunConfig, SimRunRecorder
from thenetwork.sim.scenarios import default_strong_match_configs


class ScriptedTinyPerson:
    """Deterministic stand-in for local `sim run` smoke runs."""

    def __init__(self, name: str, body: str) -> None:
        self.name = name
        self.body = body

    def listen_and_act(self, _stimulus: str):
        return {"content": self.body}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sim")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run_parser.add_argument("--ticks", type=int, default=2)
    run_parser.add_argument(
        "--proactive-every",
        type=int,
        default=0,
        help="Run proactive scans every N ticks; 0 disables them for offline smoke runs.",
    )
    compare_parser = subcommands.add_parser("compare")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)

    if args.command == "run":
        artifacts = asyncio.run(
            run_sim(
                runs_dir=args.runs_dir,
                ticks=args.ticks,
                proactive_every=args.proactive_every or None,
            )
        )
        print(artifacts.run_dir)
    elif args.command == "compare":
        print(render_compare(compare_runs(args.before, args.after)), end="")


async def run_sim(
    *,
    runs_dir: Path,
    ticks: int,
    proactive_every: int | None,
):
    configs = default_strong_match_configs()
    bodies = (
        "I need ML infrastructure help for factory operations.",
        "I deploy ML infrastructure in factory environments.",
    )
    adapters = tuple(
        TinyPersonEmailAdapter(ScriptedTinyPerson(config.name, body), config)
        for config, body in zip(configs, bodies, strict=True)
    )
    config = SimRunConfig(
        scenario="strong-match",
        ticks=ticks,
        proactive_every=proactive_every,
        personas=configs,
        mock_process=True,
    )
    return await SimRunRecorder(runs_dir=runs_dir).run(adapters, config)
