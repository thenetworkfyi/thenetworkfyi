"""Command line entrypoint for the simulation harness."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from thenetwork.sim.scoring.compare import compare_runs, render_compare
from thenetwork.sim.run.database import new_sim_database_name, provision_sim_database
from thenetwork.sim.intro_flow import run_intro_flow_sim
from thenetwork.sim.personas.persona import TinyPersonEmailAdapter
from thenetwork.sim.personas.population import (
    DEFAULT_EXPECTATIONS,
    DEFAULT_OUTCOME_CHECKS,
    PopulationPersona,
    SimSchedule,
    default_population,
)
from thenetwork.sim.run.recorder import SimRunConfig, SimRunRecorder


class ScriptedTinyPerson:
    """Deterministic stand-in for local `sim run` smoke runs."""

    def __init__(self, name: str, body: str) -> None:
        self.name = name
        self.body = body

    def listen_and_act(self, stimulus: str, *args, **kwargs):
        return {"content": self.body}


def _build_person(persona: PopulationPersona, llm_personas: bool):
    if not llm_personas:
        return ScriptedTinyPerson(persona.config.name, persona.opening_body)
    from thenetwork.model_config import model_with_api_key
    from thenetwork.settings import get_settings
    from thenetwork.sim.personas.llm_persona import LLMTinyPerson

    settings = get_settings()
    model = model_with_api_key(settings.small_agent_model, settings.small_agent_api_key)
    return LLMTinyPerson(persona.config, model)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sim")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run_parser.add_argument("--ticks", type=int, default=2)
    run_parser.add_argument(
        "--personas",
        type=int,
        default=None,
        help="Use only the first N personas of the default population.",
    )
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
    run_parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Retain the per-run Postgres database after a real-process run.",
    )
    run_parser.add_argument(
        "--llm-personas",
        action="store_true",
        help="Drive personas with SMALL_AGENT_MODEL so they hold a real conversation "
        "instead of repeating their scripted opening line.",
    )
    run_parser.add_argument(
        "--message-budget",
        type=int,
        default=None,
        help="Override each persona's max messages for the run.",
    )
    compare_parser = subcommands.add_parser("compare")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    intro_parser = subcommands.add_parser("intro-flow")
    intro_parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    intro_parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Retain the per-run Postgres database.",
    )
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.keep_db and not args.real_process:
            parser.error("--keep-db requires --real-process")
        artifacts = asyncio.run(
            run_sim(
                runs_dir=args.runs_dir,
                ticks=args.ticks,
                proactive_every=args.proactive_every or None,
                mock_process=not args.real_process,
                keep_db=args.keep_db,
                personas=args.personas,
                llm_personas=args.llm_personas,
                message_budget=args.message_budget,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        )
        print(artifacts.run_dir)
    elif args.command == "compare":
        print(render_compare(compare_runs(args.before, args.after)), end="")
    elif args.command == "intro-flow":
        artifacts = asyncio.run(
            run_intro_flow_sim(
                runs_dir=args.runs_dir,
                keep_db=args.keep_db,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        )
        print(artifacts.run_dir)


async def run_sim(
    *,
    runs_dir: Path,
    ticks: int,
    proactive_every: int | None,
    mock_process: bool = True,
    keep_db: bool = False,
    personas: int | None = None,
    llm_personas: bool = False,
    message_budget: int | None = None,
    progress: Callable[[str], None] | None = None,
):
    if keep_db and mock_process:
        raise ValueError("keep_db requires mock_process=False")
    population = default_population()
    if personas is not None:
        if personas < 1:
            raise ValueError("personas must be at least 1")
        population = population[:personas]
    if message_budget is not None:
        if message_budget < 1:
            raise ValueError("message_budget must be at least 1")
        population = tuple(
            replace(
                persona, config=replace(persona.config, message_budget=message_budget)
            )
            for persona in population
        )
    configs = tuple(persona.config for persona in population)
    adapters = tuple(
        TinyPersonEmailAdapter(_build_person(persona, llm_personas), persona.config)
        for persona in population
    )
    database_name = new_sim_database_name() if not mock_process else None
    config = SimRunConfig(
        scenario="default-population",
        ticks=ticks,
        proactive_every=proactive_every,
        personas=configs,
        mock_process=mock_process,
        expectations=DEFAULT_EXPECTATIONS,
        outcome_checks=DEFAULT_OUTCOME_CHECKS,
        llm_personas=llm_personas,
        database_name=database_name,
    )

    async def record():
        return await SimRunRecorder(runs_dir=runs_dir).run(
            adapters,
            config,
            schedule=SimSchedule.from_population(population),
            progress=progress,
        )

    if database_name is None:
        return await record()
    artifacts = None
    with provision_sim_database(
        database_name,
        keep=keep_db,
        dump_path=lambda: (
            None if artifacts is None else artifacts.raw_database_dump_path
        ),
    ):
        artifacts = await record()
    return artifacts
