"""Simulation harness experiments for The Network."""

import os

# This package imports CrewAI from several entry points. Set its process policy at
# package initialization so every submodule applies the opt-out before CrewAI loads.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
