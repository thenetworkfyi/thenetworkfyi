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

> **Status:** active development. See [`PLAN.md`](./PLAN.md) for the authoritative
> design rationale and the wave-by-wave build order. One component
> (`thenetwork/worker/proactive.py`) still references the pre-rewrite `Profile`
> schema and is not wired to the current memory model — treat proactive outreach
> as planned, not working.

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
   (`thenetwork/memory/sanitize.py`) — a deterministic PII strip (emails, phones),
   with an optional higher-fidelity LLM pass that has a fixed prompt and no tools.
   The component that sees raw cross-user data stays small and auditable; the main
   agent never self-censors.
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
DATABASE_URL=postgresql+psycopg://network:network@localhost:5432/network_db

# LLM — provider is chosen by the model string prefix
AGENT_MODEL=anthropic:claude-sonnet-4-6
EMBED_MODEL=text-embedding-3-small
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Mailbox
EMAIL_ACCOUNT=agent@example.com
EMAIL_PASSWORD=...
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587

# Tuning
WORKER_CONCURRENCY=4
RATE_LIMIT_PER_HOUR=10
CONTENT_SCAN_ENABLED=false

# Used by docker-compose for the local DB
POSTGRES_PASSWORD=network
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

### 4. Run the producer and worker

There are no console entry points yet; the producer cycle and worker are library
functions you invoke directly.

```bash
# Worker — long-running, drains the Procrastinate queue
python -c "import asyncio; from thenetwork.worker.tasks import run_worker; asyncio.run(run_worker())"

# Producer — one polling cycle (run on a timer / cron)
python -c "from thenetwork.worker.producer import run_producer_cycle; print(run_producer_cycle())"
```

In production, run these under a process supervisor (e.g. systemd
`Restart=always`) and point `DATABASE_URL` at managed Postgres (e.g. Neon).

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
  worker/     Procrastinate producer/tasks (+ proactive, WIP)
  settings.py pydantic-settings config
alembic/      migrations (vector extension lives here)
tests/        security, scenarios, pipeline
PLAN.md       authoritative design rationale + build order
```
