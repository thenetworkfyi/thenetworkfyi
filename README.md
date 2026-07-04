# The Network

An **email-driven agentic connection engine**. People email a single address; an
LLM agent reads each message, decides what (if anything) it's worth doing, and
acts — it might capture a fact about someone, surface an event to people who'd
care, introduce two people who should know each other, or simply do nothing.

There is no networking schema and no scenario script. The agent's entire
substrate is a store of **freeform memories** it reads, writes, and searches;
all behavior (onboarding, attribute capture, introductions, one-way FYIs) is
*emergent* from a system prompt plus tools, not from branching control flow.

The whole thing runs on a single VPS against Postgres.

> **Status:** active development. See [`docs/design-decisions.md`](./docs/design-decisions.md)
> for the design rationale and the list of deliberately rejected approaches. Proactive outreach
> (`thenetwork/worker/proactive.py`) is ported to the Person/Memory model and wired
> as the hourly periodic scan; it is intentionally conservative — it only enqueues
> candidate pairs and lets the agent decide whether to introduce.

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
                      | tools: remember / forget / search / dispatch_email
                      v
                  Reply --SMTP--> [Sender]
```

1. **Producer** (`thenetwork/worker/producer.py`) polls the IMAP inbox for unseen
   messages, enqueues exactly one durable Procrastinate job per message, and only
   *then* marks the message seen. Durability lives in the Postgres job row, not
   the IMAP seen-flag — a crash mid-run means the job retries and nothing is lost.
2. **Worker** (`thenetwork/worker/tasks.py`) is a Postgres-native Procrastinate
   task (LISTEN/NOTIFY + `SKIP LOCKED`, no Redis/broker). It enforces a per-sender
   rate limit and an optional content scan, looks up whether the sender is a known
   person, then runs the agent. Retries/backoff are Procrastinate's job.
3. **Agent** (`thenetwork/agent/`) is a [pydantic-ai](https://ai.pydantic.dev/)
   ReAct agent. The untrusted email body is passed as **user-role** content and
   never concatenated into the system prompt. It loops over its tools and produces
   a reply, which goes out over SMTP with the appropriate auto-reply headers.

---

## Data model

Two tables — that is the entire durable model.

### `people`
Pure identity, addressing, and the security boundary. Nothing about *why* a person
is here lives on this row.

| column | meaning |
|---|---|
| `id` (uuid str, pk) | opaque internal id — the only thing the LLM ever sees |
| `email` (unique, indexed) | resolved server-side by the mailer, never by the LLM |
| `name` | display name |

### `memories`
A growing pile of freeform chunks the agent owns.

| column | meaning |
|---|---|
| `id` (uuid str, pk) | |
| `text` | freeform content — the source of truth |
| `embedding` `Vector(1536)` | pgvector, HNSW cosine, for semantic recall |
| `refs` `text[]` | the `people.id`s this memory concerns (0..N) |
| `gist` | PII-stripped summary; the *only* thing cross-user search may return |
| `created_at` | recency / perishability signal |

The cardinality of `refs` is the only structure, and it's incidental:

- **0 refs** → general knowledge / agent notes (e.g. "a Rust meetup Thursday")
- **1 ref** → an attribute about a person ("just moved to Berlin")
- **2+ refs** → *also* contributes an undirected edge between those people

There is no `kind`/`direction`/`status`/`category`/`tags` column. Those are
distinctions the agent draws from `text` at reasoning time, not facts the DB
enforces.

### The graph is a projection, not a table
"Who knows whom" is derived: nodes are people, an edge exists between two people
because some memory references both, and edge weight comes from the count/recency
of shared memories. [NetworkX](https://networkx.org/) does the multi-hop proximity
math at query time (`thenetwork/search/graph.py`); the LLM does the language →
reference mapping at write time.

---

## Agent surface

Memory is CRUD exposed as tools; everything else is emergent behavior
(`thenetwork/agent/tools.py`).

| tool | description |
|---|---|
| `remember(text, refs)` | write a chunk; a PII-stripped gist is produced automatically for any memory with refs |
| `forget(memory_id)` | delete a chunk (edit = forget + remember, so embeddings never go stale) |
| `search(query) -> [{person_id, gist, similarity}]` | semantic recall returning **opaque ids + gist only** for other people |
| `dispatch_email(recipient_user_id, subject, body, …)` | opaque id in; the real address is resolved server-side at send time |

---

## Security model — THE SEAL

The critical concern: **prompt injection must not be able to exfiltrate user
identities or data, yet the agent must still email people on a user's behalf.**
Leakage is made *structurally impossible* rather than prompt-dependent.

In a freeform store, a memory like *"Bob (bob@x.com) is a Rust dev looking for a
cofounder"* is raw PII inside `text`. Returning it for anyone but Bob would let a
prompt-injection exfiltrate it, so the privacy boundary cannot be "withhold a
column." Instead:

1. **Two-layer memory for person-referencing chunks.** Each carries a **raw form**
   (retrievable only for that person's own requests) and a **sanitized gist**
   (PII-stripped) that is the only thing cross-user search may return.
2. **Cross-user retrieval and the LLM only ever touch the gist + opaque ids.** A
   hijacked model has no identifying text to leak. Real addresses never enter LLM
   context — the mailer resolves them server-side.
3. **Self/other gate** (`thenetwork/memory/seal.py`): sole-ref-is-sender → raw
   text; otherwise → gist only.
4. **The sanitizer is a separate, narrowly-scoped step**
   (`thenetwork/memory/sanitize.py`) — mandatory Presidio redaction of names, email
   addresses, and phone numbers while keeping organizations and locations for search
   recall, plus an optional higher-fidelity LLM pass that has a fixed prompt and no
   tools. Missing Presidio is a deployment error, not a silent downgrade. The component
   that sees raw cross-user data stays small and auditable; the main agent never
   self-censors.
5. **Capability-style email tool (confused-deputy fix).** `dispatch_email` takes
   an opaque `recipient_user_id`; the address is resolved server-side at send time.
   The LLM never sees or supplies a raw address.
6. **Role separation.** The untrusted inbound body is passed as user-role message
   content, never into the system prompt (`thenetwork/agent/core.py`).
7. **Mail-loop prevention (RFC 3834).** Inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*` is skipped; all outbound sets
   `Auto-Submitted: auto-replied`.
8. **Rate limiting / anti-DoS.** Per-sender quota via
   [`limits`](https://limits.readthedocs.io/) (Postgres-backed), plus bounded
   Procrastinate worker concurrency as the global LLM-spend ceiling.
9. **Credentials.** Never hardcoded — loaded from env / `.env` via
   pydantic-settings.
10. **Optional content scanner.** Provider moderation / LLM Guard as opt-in
    defense-in-depth, never the primary defense.

The red-team suite (`tests/security/`) proves it: adversarial emails must produce
**zero** raw other-person memory text — no names, emails, or bios — in the reply
*or* in any tool argument, even under a fully-hijacked model.

---

## Stack

| Concern | Choice |
|---|---|
| ORM / models | SQLModel over psycopg 3 (`postgresql+psycopg://`) |
| Migrations | Alembic (`CREATE EXTENSION vector` runs idempotently in a migration) |
| Vector store | pgvector — `Vector(1536)`, HNSW cosine |
| Agent | pydantic-ai (native multi-provider; provider chosen by config string) |
| Embeddings | provider-agnostic `embed_text` wrapper |
| Graph proximity | NetworkX |
| Settings | pydantic-settings (`BaseSettings`, env / `.env`) |
| IMAP | imap-tools |
| SMTP | stdlib `email.message.EmailMessage` + `smtplib` |
| Job queue | Procrastinate (Postgres-native; no broker/Redis) |
| Rate limiting | `limits` |
| Tests | pytest + pydantic-evals |

Vendor-agnosticism comes from pydantic-ai and the embedding wrapper both being
multi-provider, selected by the `AGENT_MODEL` / `EMBED_MODEL` config strings — no
LiteLLM, no proxy glue.

---

## Getting started

### Prerequisites
- Python 3.12+
- Docker (for local Postgres) or a managed Postgres with the `vector` extension
- An LLM provider key (`OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`)
- An IMAP/SMTP mailbox for the agent

### 1. Configure

Create a `.env` in the project root. Defaults live in `thenetwork/settings.py`;
the common overrides:

```dotenv
POSTGRES_HOST=localhost   # docker compose overrides this to `db` for the worker
POSTGRES_PORT=5432
POSTGRES_DB=network_db
POSTGRES_USER=network
POSTGRES_PASSWORD=network   # literal password; Settings.database_url percent-encodes it

# LLM — provider is chosen by the model string prefix
AGENT_MODEL=anthropic:claude-sonnet-5
EMBED_MODEL=text-embedding-3-small
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Mailbox — IMAP (inbound polling) and SMTP (outbound send) are separate
# accounts/credentials, potentially on different providers
IMAP_ACCOUNT=agent@example.com
IMAP_PASSWORD=...
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
SMTP_ACCOUNT=agent@example.com
SMTP_PASSWORD=...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587

# Tuning
WORKER_CONCURRENCY=4
RATE_LIMIT_PER_HOUR=10
UNAUTHENTICATED_RATE_LIMIT_PER_HOUR=3
GLOBAL_EMAIL_RATE_LIMIT_PER_HOUR=100
CONTENT_SCAN_ENABLED=false
```

### 2. Install

```bash
pip install -e .
# optional content scanner:
# pip install -e ".[content-scan]"
```

### 3. Start Postgres and apply migrations

```bash
docker compose up -d db        # local pgvector/pgvector:pg17
alembic upgrade head           # creates the vector extension + tables
```

### 4. Run the worker

The worker is a single long-running process. It drains the Procrastinate queue
and, via periodic tasks, polls the IMAP inbox every minute (`poll_inbox`) and
runs the hourly proactive scan (`scan_for_opportunities`) — no separate producer
process is needed.

```bash
thenetwork-worker            # long-running: intake + processing + scans
thenetwork-producer          # optional: one manual IMAP poll cycle
```

---

## Deployment

This service needs **no inbound network access** — it polls IMAP (outbound),
pulls jobs from local Postgres, and calls LLM/SMTP APIs (outbound). So there's
no web server, reverse proxy, or public port to expose. A single small VPS with
SSH access is enough.

### Single-VPS Docker Compose

`docker-compose.yml` runs two services: `db` (pgvector Postgres, bound to
`127.0.0.1` only, state in the `pgdata` volume) and `worker`. Postgres on the
box is fine at this scale.

```bash
cp .env.example .env          # fill in secrets
docker compose up -d --build  # builds the image, starts db + worker
docker compose logs -f worker
```

The container entrypoint runs `alembic upgrade head` before starting, so
migrations apply automatically on every deploy.

### Safe redeploys (no lost or half-processed jobs)

Procrastinate makes this safe by design — durable job rows in Postgres,
`SKIP LOCKED` dequeue, and graceful shutdown on SIGTERM (the worker stops
fetching new jobs and finishes in-flight ones before exiting). `process_email`
also retries (`max_attempts=3`) and the intake is idempotent (IMAP messages are
marked seen only *after* enqueue). So even an ungraceful kill only re-runs a job;
nothing is lost. `stop_grace_period: 300s` in compose gives in-flight agent runs
time to drain.

A deploy is therefore just:

```bash
docker compose pull && docker compose up -d   # recreates only changed services
```

### Pushing new images

`.github/workflows/publish.yml` builds and pushes to GHCR
(`ghcr.io/<owner>/<repo>`) on every push to `main` and on `v*` tags. On the
server, set `IMAGE` in `.env` to that path and run the deploy command above.
(`ghcr.io` images may need `docker login ghcr.io` once if the package is private.)

### Backups

The DB is the only source of truth. `scripts/backup.sh` dumps it via the `db`
container; install it as a host cron job (example invocation is in the script).

---

## Tests

```bash
pytest                       # full suite
pytest -m "not integration"  # skip tests that need a live pgvector DB
```

- `tests/security/` — the SEAL red-team and security contracts
- `tests/scenarios/` — emergent-behavior evals (pydantic-evals)
- `tests/test_match_pipeline.py` — semantic match / search pipeline

---

## Layout

```
thenetwork/
  agent/      pydantic-ai agent: core wiring, tools, prompts, deps
  db/         SQLModel models + session
  email/      IMAP inbound polling, SMTP outbound
  embed/      provider-agnostic embedding wrapper
  memory/     the SEAL: sanitize (gist) + seal (self/other gate)
  search/     semantic match over memories + NetworkX graph projection
  security/   rate limiting + optional content scan
  worker/     Procrastinate producer/tasks + proactive (hourly scan)
  settings.py pydantic-settings config
alembic/      migrations (vector extension lives here)
tests/        security, scenarios, pipeline
CLAUDE.md     guidance for Claude Code (imports docs/ for architecture, the SEAL, rationale)
```
