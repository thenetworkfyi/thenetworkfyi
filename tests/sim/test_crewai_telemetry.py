"""CrewAI telemetry policy tests for simulation entry points."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module_name",
    ("thenetwork.sim.cli", "thenetwork.sim.run.crew_flow"),
)
def test_sim_entry_point_disables_telemetry_before_crewai_import(module_name):
    script = f"""
import builtins
import os

class CrewAIImportObserved(BaseException):
    pass

real_import = builtins.__import__

def checked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "crewai" or name.startswith("crewai."):
        assert os.environ.get("CREWAI_DISABLE_TELEMETRY") == "true"
        assert os.environ.get("CREWAI_TESTING") == "true"
        raise CrewAIImportObserved
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = checked_import

try:
    import {module_name}
except CrewAIImportObserved:
    pass
else:
    raise AssertionError("entry point did not import CrewAI")
"""
    env = os.environ.copy()
    env.pop("CREWAI_DISABLE_TELEMETRY", None)
    env.pop("CREWAI_TESTING", None)

    subprocess.run([sys.executable, "-c", script], env=env, check=True)
