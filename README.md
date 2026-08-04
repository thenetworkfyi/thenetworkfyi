# The Network

[![CI](https://github.com/thenetworkfyi/thenetworkfyi/actions/workflows/ci.yml/badge.svg)](https://github.com/thenetworkfyi/thenetworkfyi/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An **email-driven agentic connection engine**. People email a single address; an
LLM agent reads each message, decides what to do about it, and acts: it might
capture a fact about someone, surface an event to people who'd care, introduce
two people who should know each other, or do nothing.

There is no networking schema and no scenario script. The agent's entire
substrate is a store of **freeform memories** it reads, writes, and searches;
all behavior (onboarding, attribute capture, introductions, one-way FYIs) is
*emergent* from a system prompt plus tools, not from branching control flow.


---

## How it works

```
[Inbox] --IMAP--> Producer (imap-tools poll)
                      | enqueue ONE Procrastinate job per message, THEN mark seen
                      v
              [Postgres job row]  <- durable source of truth
                      | LISTEN/NOTIFY + SKIP LOCKED
                      v
                  Worker (Procrastinate task)
                      | rate-limit + optional content scan
                      | run the pydantic-ai agent: Think / Act / Observe
                      | tools: memory + introductions + event lifecycle /
                      |        sealed email and event capabilities
                      v
                Outbound --SMTP--> [Recipient]

[Dovecot catch-all: hidden-*@RELAY_DOMAIN]
                      | configured IMAP inbox/job path
                      v
              server-owned pair resolver --SES/SMTP--> [Other participant]
```

1. **Producer** (`thenetwork/worker/producer.py`) polls the primary and optional relay
   IMAP inboxes for unseen messages, enqueues exactly one durable Procrastinate job per
   message, and only *then* marks the message seen. Durability lives in the Postgres job
   row, not the IMAP seen-flag: a crash mid-run means the job retries and nothing is
   lost. Disposable domains are rejected before enqueue. Optional burst monitoring can
   pause only primary intake before a suspicious batch is marked seen; relay and verified
   PGP admin mail continue.
2. **Worker** (`thenetwork/worker/tasks.py`) is a Postgres-native Procrastinate
   task (LISTEN/NOTIFY + `SKIP LOCKED`, no Redis/broker). It enforces a per-sender
   rate limit and an optional content scan, looks up whether the sender is a known
   person, then runs the agent. Retries/backoff are Procrastinate's job.
3. **Agent** (`thenetwork/agent/`) is a [pydantic-ai](https://ai.pydantic.dev/)
   ReAct agent. The untrusted email body is passed as **user-role** content and
   never concatenated into the system prompt. It loops over its tools and may remember,
   reply, send outreach, manage an event, propose an introduction, escalate, or record
   that no action is warranted.

Introduced pairs communicate through one stable
`hidden-<reply-token>@RELAY_DOMAIN` address. The existing Dovecot catch-all puts
mail for those addresses into either the primary IMAP mailbox or an optional
separately authenticated relay mailbox. The worker authenticates the sender, requires
the pair to remain `introduced`, resolves only the other participant, and resends
through the existing SES/SMTP connection before any agent execution. This hides
participant email addresses. The fixed introduction also omits participant names, prints
the relay address in its body, and includes a sanitized match recap. Subject and body
content that participants later send through the relay are not anonymized or rewritten.
It adds no webhook, inbound HTTP endpoint, or separate receiving service.

---

## Data model

Two tables carry the domain. `people` is pure identity and addressing: an opaque `id`
(the only person reference the LLM ever sees), an `email` the mailer resolves
server-side, and a name. `memories` is the substrate: freeform `text`, a
`Vector(1536)` embedding for semantic recall, a `refs` array of the `people.id`s the
chunk concerns, a PII-stripped `gist`, and `created_at`.

There is no `kind`/`direction`/`status`/`category`/`tags` column. Those are distinctions
the agent draws from `text` at reasoning time, not facts the DB enforces. The cardinality
of `refs` is the only structure, and it's incidental: zero refs is a general note, one
ref is an attribute about a person, and two or more *also* contributes an undirected edge
between them.

That last case is where the social graph comes from. "Who knows whom" is never stored:
nodes are people, an edge exists because some memory references both, and edge weight
comes from the count and recency of shared memories.
[NetworkX](https://networkx.org/) does the multi-hop proximity math at query time
(`thenetwork/search/graph.py`); the LLM does the language → reference mapping at write
time.

A handful of narrow operational tables enforce what cannot safely be left to model
reasoning: consent and the relay token, event lifecycle and delivery deduplication,
rate limits, intake idempotency, bans, PGP replay protection, and the intake circuit
breaker. They are security and lifecycle state, not a networking schema over memories.
Procrastinate owns its own durable queue tables in the same database.

See [`docs/architecture.md`](./docs/architecture.md) for the full column-level model and
the agent's seventeen tools; the tools themselves are defined in
`thenetwork/agent/tools.py`.

---

## Security model: THE SEAL

The critical concern: **prompt injection must not be able to exfiltrate user
identities or data, yet the agent must still email people on a user's behalf.**
Leakage is made *structurally impossible* rather than prompt-dependent.

In a freeform store, a memory like *"Bob (bob@x.com) is a Rust dev looking for a
cofounder"* is raw PII inside `text`. Returning it for anyone but Bob would let a
prompt-injection exfiltrate it, so the privacy boundary cannot be "withhold a
column."

Every person-referencing memory carries two layers: the raw text, read only by the
sanitizer and the PGP-verified admin channel, and a PII-stripped **gist**. Cross-user
retrieval and the LLM only ever touch gists and opaque ids, so a hijacked model has no
identifying text to leak. The search projections are the chokepoint: their SQL selects
sanitized gists and opaque ids and nothing else, so there is no runtime branch an
attacker can steer toward raw text.

The email tools are capabilities rather than free-form senders. `reply_to_sender` has no
recipient argument at all and derives one from the authenticated inbound sender;
`send_outreach` takes an opaque id the mailer resolves server-side; introductions require
authenticated double opt-in and are composed entirely by server code. The LLM never sees
or supplies a raw address. The untrusted inbound body is user-role message content and is
never concatenated into the system prompt.

[`docs/security.md`](./docs/security.md) has the full threat model: all fourteen layers,
the sanitizer's redaction policy, the anonymous relay, rate limiting and the intake
circuit breaker, PII-safe audit correlation, the optional content scanner, and the
separate PGP-verified admin channel.

The red-team suite (`tests/security/`) proves it: adversarial emails must produce
**zero** raw other-person memory text (no names, emails, or bios) in the reply
*or* in any tool argument, even under a fully-hijacked model.

---

## Stack

| Concern | Choice |
|---|---|
| ORM / models | SQLModel over psycopg 3 (`postgresql+psycopg://`) |
| Migrations | Alembic (`CREATE EXTENSION vector` runs idempotently in a migration) |
| Vector store | pgvector with `Vector(1536)`, HNSW cosine |
| Agent | pydantic-ai (native multi-provider; provider chosen by config string) |
| Embeddings | OpenAI text embeddings, fixed at 1536 dimensions |
| Graph proximity | NetworkX |
| Settings | pydantic-settings (`BaseSettings`, env / `.env`) |
| IMAP | imap-tools |
| SMTP | stdlib `email.message.EmailMessage` + `smtplib` |
| Job queue | Procrastinate (Postgres-native; no broker/Redis) |
| Rate limiting | `limits` |
| Tests | pytest + pydantic-evals |

Vendor-agnosticism comes from pydantic-ai, selected by the `AGENT_MODEL` config
string, with no LiteLLM or proxy glue. Embeddings use OpenAI only: `EMBED_MODEL` must
be `text-embedding-3-small`, `text-embedding-3-large` (each requested at 1536
dimensions), or legacy `text-embedding-ada-002` (native 1536 dimensions), matching
the database's `Vector(1536)` column.

---

## Getting started

### Prerequisites
- Python 3.12+
- Docker (for local Postgres) or a managed Postgres with the `vector` extension
- API keys for the configured agent, small-agent, and embedding models
- An IMAP/SMTP mailbox for the agent
- A Dovecot catch-all for the relay domain, delivered into that IMAP mailbox

For the complete mail-host, SES, application, and deployment validation procedure, see
[Hidden-address email relay setup](docs/email-relay-setup.md).

### 1. Configure

```bash
cp .env.example .env
```

`.env.example` is the annotated list of every setting; defaults live in
`thenetwork/settings.py`. At minimum you need Postgres credentials, the three model
strings and their API keys (the provider is chosen by the model string's prefix), the
IMAP and SMTP mailbox credentials, and `RELAY_DOMAIN`.

### 2. Install

```bash
uv pip install -e .
```

The gist sanitizer uses `openai/privacy-filter`, which is Apache 2.0 and ungated,
so it needs no account or token. A local `uv run` pulls its weights (~2.7 GB) into
your own Hugging Face cache on first use. The Docker image bakes them in at build
time instead, so a deployed worker downloads nothing at startup.

The project installs pinned LlamaFirewall scanner dependencies and uses
`meta-llama/Llama-Prompt-Guard-2-86M`, a gated model under the Llama 4 Community
License. Accept that license on Hugging Face and set `HF_TOKEN` before the first
enabled worker startup. `CONTENT_SCAN_ENABLED` is the only feature switch: when
enabled, startup downloads and initializes the model before queue processing;
later starts use `HF_HOME` and need no token. LlamaFirewall brings PyTorch and
its platform wheels, so budget extra image/disk space and memory even when
scanning is disabled at runtime. The image installs the CPU-only torch build,
since the deployment target has no GPU.

### 3. Start Postgres and apply migrations

```bash
docker compose up -d db        # local pgvector/pgvector:pg17
uv run alembic upgrade head     # creates the vector extension + tables
```

### 4. Run the worker

The worker is a single long-running process. It drains the Procrastinate queue
and, via periodic tasks, polls IMAP every minute (`poll_inbox`) and runs three hourly
discovery scans: graph people matching (`scan_for_opportunities`), semantic people
rematching (`scan_for_matches`), and semantic event recommendations
(`scan_for_event_recommendations`). When primary monitoring is enabled, the same worker
also runs the fixed-policy abuse judge at minute 15; it is not a discovery scan and cannot
send user mail or use tools. No separate producer process is needed.

```bash
uv run thenetwork-worker            # long-running: intake + processing + scans
uv run thenetwork-producer          # optional: one manual IMAP poll cycle
```

---

## Deployment

This service needs **no application-owned inbound network access**. It polls IMAP
(outbound), pulls jobs from local Postgres, and calls LLM/SMTP APIs (outbound). You expose
no web server, reverse proxy, or public port, so a host with SSH access is all the deploy
needs. Hidden-address replies arrive through the existing Dovecot catch-all mailbox, not
an application endpoint.

### Docker Compose

`docker-compose.yml` runs `db` (pgvector Postgres, bound to `127.0.0.1` only, state in
the `pgdata` volume), `worker`, and the observability services below. Postgres on the box
is fine at this scale.

```bash
cp .env.example .env          # fill in secrets
docker compose up -d --build  # builds the image, starts the stack
docker compose logs -f worker
```

The container entrypoint runs `alembic upgrade head` before starting, so
migrations apply automatically on every deploy.

### Observability

The compose stack also runs pinned OpenTelemetry Collector, Loki, Prometheus, Alertmanager,
and Grafana services. Worker JSON logs reach the Collector over Docker's `fluentd` driver;
it forwards every line to Loki and derives a bounded catalog of Prometheus counters from
redacted audit records on the same pipeline. All four UIs bind to `127.0.0.1` only, the
worker opens no metrics port, and no external telemetry backend is required.

[docs/monitoring.md](docs/monitoring.md) has the metric catalog and semantics, the label
allow-list, alert thresholds, routing, runbooks, and the per-file validation sequence.

### Safe redeploys (no lost or half-processed jobs)

Procrastinate keeps durable job rows in Postgres, dequeues with `SKIP LOCKED`, and shuts
down gracefully on SIGTERM (the worker stops fetching new jobs and finishes in-flight ones
before exiting). `process_email` also retries (`max_attempts=3`) and the intake is
idempotent (IMAP messages are marked seen only *after* enqueue), so an ungraceful kill at
worst re-runs a job and loses nothing. `stop_grace_period: 300s` in compose gives
in-flight agent runs time to drain.

A deploy is therefore:

```bash
export IMAGE=ghcr.io/thenetworkfyi/thenetworkfyi:<commit-sha>
git pull origin main && docker compose pull worker && docker compose up -d
```

`.github/workflows/ci.yml` runs exactly that over SSH (`deploy` job,
`environment: production`) on every push to `main`, once the `test` job passes and a
separate `build` job has pushed the public worker image to GHCR (package settings set to
public visibility; audit verified images contain no secrets). The VPS never builds the
image itself. It pulls the immutable commit-SHA tag CI produced, leaving the server's
resources for serving. The deploy commands live inline in the workflow file rather than a
script checked out on the server, so every run executes the version from the commit that
passed CI rather than whatever sits on disk. After deployment, CI retains the three newest
GHCR package versions and deletes older versions.

### Backups

The DB is the only source of truth. `scripts/backup.sh` dumps it via the `db`
container; install it as a host cron job (example invocation is in the script).

---

## Tests

```bash
uv run pytest                       # full suite
uv run pytest -m "not integration"  # skip tests that need a live pgvector DB
```

- `tests/security/`: the SEAL red-team and security contracts
- `tests/scenarios/`: emergent-behavior evals (pydantic-evals)
- `tests/test_match_pipeline.py`: semantic match / search pipeline

---

## Layout

```
thenetwork/
  admin/      PGP/MIME-verified operator commands
  agent/      pydantic-ai agent: core wiring, tools, prompts, deps
  db/         SQLModel models + session
  email/      IMAP intake, SMTP output, hidden-address relay, templates
  embed/      OpenAI embedding wrapper (1536 dimensions)
  memory/     gist sanitization, recent-context, and sent-email memory helpers
  search/     semantic match over memories + NetworkX graph projection
  security/   rate limiting + optional content scan
  sim/        deterministic and model-driven end-to-end simulation harness
  worker/     Procrastinate intake/tasks + people and event discovery scans
  settings.py pydantic-settings config
alembic/      migrations (vector extension lives here)
docs/         architecture, security, development, operations, and review runbooks
tests/        unit, security, scenario, simulation, and integration suites
AGENTS.md     repository guidance (`CLAUDE.md` is a symlink to this file)
```
