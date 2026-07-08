"""Command line entrypoint for the simulation harness."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from thenetwork.sim.compare import compare_runs, render_compare
from thenetwork.sim.persona import TinyPersonEmailAdapter
from thenetwork.sim.population import SimSchedule, default_population
from thenetwork.sim.recorder import SimRunConfig, SimRunRecorder


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
    run_parser.add_argument(
        "--real-process",
        action="store_true",
        help="Route each turn through the real process_email task instead of the mock stub.",
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
                mock_process=not args.real_process,
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
    mock_process: bool = True,
):
    population = default_population()
    configs = tuple(persona.config for persona in population)
    adapters = tuple(
        TinyPersonEmailAdapter(
            ScriptedTinyPerson(persona.config.name, persona.opening_body),
            persona.config,
        )
        for persona in population
    )
    config = SimRunConfig(
        scenario="default-population",
        ticks=ticks,
        proactive_every=proactive_every,
        personas=configs,
        mock_process=mock_process,
    )
    return await SimRunRecorder(runs_dir=runs_dir).run(
        adapters,
        config,
        schedule=SimSchedule.from_population(population),
    )
