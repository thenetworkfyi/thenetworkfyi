# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**The Network** - an email-driven agentic connection engine. People email a single
address; a pydantic-ai agent reads each message, decides what (if anything) to do, and
acts: capture a fact, surface an event, introduce two people, or do nothing. There is
no networking schema and no scenario script - all behavior is *emergent* from a system
prompt plus six tools over a store of freeform **memories**. Runs as a single
long-lived worker on one VPS against Postgres (pgvector). No inbound network access.

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
pip install -e ".[dev]"            # install with test deps
pip install -e ".[content-scan]"   # optional content scanner (llm-guard)
python -m spacy download en_core_web_lg  # required Presidio model for local worker runs; Docker bakes it in

docker compose up -d db            # local pgvector Postgres
alembic upgrade head               # create vector extension + tables

thenetwork-worker                  # long-lived process: intake + processing + scans
thenetwork-producer                # one manual IMAP poll cycle

pytest                             # full suite
pytest -m "not integration"        # skip tests needing a live pgvector DB (what CI runs)
pytest tests/security/             # the SEAL red-team + contracts
pytest tests/test_match_pipeline.py::test_name   # a single test
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
- `thenetwork/worker/proactive.py` holds two hourly periodic scans, both of which only
  `defer` a synthetic `process_email` job per candidate pair - they never introduce
  people themselves, the agent decides. `scan_for_opportunities` finds high graph-proximity
  pairs; `scan_for_matches` is the semantic rematch that re-engages a dormant user when a
  later arrival finally matches an older standing note (SEAL-safe trigger body: opaque ids
  + gists only). Both covered by `tests/test_proactive.py`.
