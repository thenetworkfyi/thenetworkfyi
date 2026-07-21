# AGENTS.md

This file provides guidance to coding agents working with this repository. `CLAUDE.md`
symlinks here for Claude Code compatibility.

## What this is

**The Network** - an email-driven agentic connection engine. People email a single
address; a pydantic-ai agent reads each message, decides what (if anything) to do, and
acts: capture a fact, surface an event, introduce two people, or do nothing. There is
no networking schema and no scenario script - all behavior is *emergent* from a system
prompt plus sixteen tools over a store of freeform **memories**. Runs as a single
long-lived worker on one VPS against Postgres (pgvector). No application-owned inbound
network access.

The README has the full prose; the docs below are the working details. The one rule that
shapes everything: hand-write only the genuine domain glue (memory + the privacy seal);
for queues, ORM, rate limiting, embeddings, graph math, and mail, use the established
library. Most "cleaner schema" ideas were already considered and rejected - see
@docs/design-decisions.md before proposing one.

## The non-negotiable invariant: THE SEAL

The central design constraint - **prompt injection must not be able to exfiltrate
user identities or data, even under a fully-hijacked model.** Leakage is made
*structurally impossible*, not prompt-dependent. Before touching anything in
`agent/`, `memory/`, `search/`, or `email/`, read @docs/security.md. Changes there
must keep the `tests/security/` red-team suite green.

## Commands

```bash
uv pip install -e ".[dev]"         # install with test deps, including Presidio and the content scanner

docker compose up -d db            # local pgvector Postgres
uv run alembic upgrade head         # create vector extension + tables

uv run thenetwork-worker            # long-lived process: intake + processing + scans
uv run thenetwork-producer          # one manual IMAP poll cycle

uv run pytest                       # full suite
uv run pytest -m "not integration"  # skip tests needing a live pgvector DB (what CI runs)
uv run pytest tests/security/       # the SEAL red-team + contracts
uv run pytest tests/test_match_pipeline.py::test_name   # a single test
uv run --extra dev ruff check .     # lint the repository
uv run --extra dev ruff format .    # format the repository
uv run --extra dev ruff format --check .  # verify formatting without changes
```

CI (`.github/workflows/ci.yml`) runs only `pytest -m "not integration"` on Python 3.12 -
it has no database, so anything DB-backed must be marked `integration`.

## Topic docs

- @docs/architecture.md - message flow, data model, the graph projection, agent surface
- @docs/security.md - THE SEAL: the layers, the gate, what the red-team enforces
- @docs/development.md - settings, migrations, test fixtures, deployment, proactive scan, sharp edges
- @docs/design-decisions.md - the *why*: guiding principle + the list of deliberately rejected approaches

## Tone

Never use "!" or other fake happy copy (e.g. "Great!", "Awesome!", "You're all set!") in
code, commit messages, docs, or responses. Keep it straightforward and professional.

## Gotchas worth knowing up front

- `DATABASE_URL` uses the `postgresql+psycopg://` (SQLModel) form. Procrastinate needs
  the plain `postgresql://` form - `tasks.run_worker` strips the `+psycopg` itself; don't
  pass the SQLModel DSN to Procrastinate directly.
- Editing a memory = `forget` + `remember` (never mutate in place), so embeddings and
  gists never go stale.
- Periodic discovery has three independent hourly scans. `thenetwork/worker/proactive.py`
  holds the two people scans: `scan_for_opportunities` finds high graph-proximity pairs,
  while `scan_for_matches` semantically revisits standing notes for dormant or unengaged
  users. The graph trigger body contains opaque person ids plus a proximity score; the
  semantic trigger body contains opaque ids, sealed gists, and similarity. Both only
  `defer` synthetic `process_email` jobs and never introduce people themselves.
  `thenetwork/worker/event_scan.py` independently matches active sealed event gists to
  people and defers triggers with opaque ids, sealed gists, expiry/similarity, and a
  server-bound event version. Neither people scan has a dedicated whole-scan fan-out cap;
  the semantic scan bounds its input and top-k, while the event scan also has whole-scan
  and per-person limits. Server capabilities recheck eligibility before sending.
