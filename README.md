# The Network

An **email-driven agentic connection engine**. People email a single address; an
LLM agent reads each message, decides what (if anything) it's worth doing, and
acts - it might capture a fact about someone, surface an event to people who'd
care, introduce two people who should know each other, or simply do nothing.

There is no networking schema and no scenario script. The agent's entire
substrate is a store of **freeform memories** it reads, writes, and searches;
all behavior (onboarding, attribute capture, introductions, one-way FYIs) is
*emergent* from a system prompt plus tools, not from branching control flow.

The whole thing runs on a single VPS against Postgres.

> **Status:** active development. See [`docs/design-decisions.md`](./docs/design-decisions.md)
> for the design rationale and the list of deliberately rejected approaches. Three
> independent hourly scans surface graph matches, semantic people matches, and relevant
> events. The scans only enqueue sealed candidates; the agent decides whether to act, and
> server-owned capabilities enforce what can leave the system. An optional separate hourly
> judge can pause primary intake from sealed cross-account abuse patterns.

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
   row, not the IMAP seen-flag - a crash mid-run means the job retries and nothing is
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
   reply, send outreach, manage an event, propose an introduction, escalate, or explicitly
   take no action.

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

`people` and `memories` are the core domain substrate. Narrow operational tables enforce
security and lifecycle invariants that cannot safely be left to model reasoning; they do
not turn freeform memories into a networking schema. Procrastinate also owns its durable
queue tables in the same Postgres database.

### `people`
Pure identity, addressing, and the security boundary. Nothing about *why* a person
is here lives on this row.

| column | meaning |
|---|---|
| `id` (uuid str, pk) | opaque internal id - the only thing the LLM ever sees |
| `email` (unique, indexed) | resolved server-side by the mailer, never by the LLM |
| `name` | display name |

### `memories`
A growing pile of freeform chunks the agent owns.

| column | meaning |
|---|---|
| `id` (uuid str, pk) | |
| `text` | freeform content - the source of truth |
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

### Operational and security state

- `introduction_consents` stores server-owned pairwise consent, decline/revocation,
  sanitized proposal snapshots, and the opaque token used by the hidden-address relay.
- `proactive_surfaces` rotates recently surfaced people pairs so scans can reach later
  candidates without repeatedly emailing the same pair.
- `events`, `event_recommendations`, and `event_suppressions` enforce stable event identity,
  ownership, lifecycle, version-bound delivery deduplication, and event-only opt-out. Event
  meaning remains freeform.
- `rate_limits`, `processed_messages`, `banned_emails`, and `admin_nonces` provide durable
  abuse controls, intake idempotency, bans, and PGP-admin replay protection.
- `primary_intake_state`, `primary_intake_observations`, and
  `primary_intake_judge_state` implement the primary-only circuit breaker using keyed
  fingerprints and an idempotent judge cursor, never raw campaign content.

### The graph is a projection, not a table
"Who knows whom" is derived: nodes are people, an edge exists between two people
because some memory references both, and edge weight comes from the count/recency
of shared memories. [NetworkX](https://networkx.org/) does the multi-hop proximity
math at query time (`thenetwork/search/graph.py`); the LLM does the language →
reference mapping at write time.

---

## Agent surface

The agent has sixteen tools (`thenetwork/agent/tools.py`):

| tool | description |
|---|---|
| `remember(text, refs)` | write a chunk; a PII-stripped gist is produced automatically for any memory with refs |
| `forget(memory_id)` | delete a sender-owned, single-ref chunk (edit = forget + remember, so embeddings never go stale) |
| `search(query) -> [{person_id, gist, similarity}]` | semantic recall returning **opaque ids + gist only** for other people |
| `reply_to_sender(subject, body_text, sent_email_summary)` | reply only to the registered inbound sender; after SMTP succeeds, remember only the separate concise summary |
| `send_outreach(recipient_user_id, subject, body_text, sent_email_summary)` | send unthreaded outreach by opaque id and remember only the post-SMTP summary; resolve the address server-side |
| `propose_introduction(other_person_id, sender_gist, other_gist)` | create a sealed pairwise proposal; server-owned consent controls the anonymous relay handoff |
| `register_person(name)` | self-register an authenticated first-contact sender; the server supplies the sender address |
| `escalate(reason)` | flag the inbound email for human review; authenticated unknown senders receive fixed first-contact guidance instead |
| `no_action(reason)` | explicitly end a run without dispatching or mutating anything |
| `create_event(text, expires_at, recurrence)` | create a sender-owned event or recurring series with a sealed cross-user gist |
| `update_event(event_id, text, expires_at, recurrence)` | replace an owned event while retaining its stable id and refreshing its version, gist, and embedding |
| `cancel_event(event_id)` | cancel an owned event so it cannot be searched or recommended |
| `search_events(query)` | search active events through an opaque-id + sealed-gist projection |
| `send_event_recommendation(event_id)` | send only the scan-bound event to the current proactive recipient using server-composed copy |
| `stop_event_recommendations()` | suppress event FYIs for the authenticated sender without affecting people matching |
| `resume_event_recommendations()` | remove only the authenticated sender's event-FYI suppression |

---

## Security model - THE SEAL

The critical concern: **prompt injection must not be able to exfiltrate user
identities or data, yet the agent must still email people on a user's behalf.**
Leakage is made *structurally impossible* rather than prompt-dependent.

In a freeform store, a memory like *"Bob (bob@x.com) is a Rust dev looking for a
cofounder"* is raw PII inside `text`. Returning it for anyone but Bob would let a
prompt-injection exfiltrate it, so the privacy boundary cannot be "withhold a
column." Instead:

1. **Two-layer memory for person-referencing chunks.** Each carries a **raw form**
   (the durable substrate, read only by the sanitizer and the PGP-verified admin
   channel) and a **sanitized gist** (PII-stripped) that is the only form any
   search may return.
2. **Cross-user retrieval and the LLM only ever touch gists + opaque ids.** A
   hijacked model has no identifying text to leak. This applies to person memories and
   events; real addresses and event submitter identities never enter LLM context.
3. **Search projections are the chokepoints** (`thenetwork/search/match.py`,
   `thenetwork/search/events.py`): their SQL selects only sanitized gists, opaque ids,
   and minimum lifecycle fields. Raw memory/event text, raw event recurrence, and event
   submitter identity never enter a cross-user result set.
4. **The sanitizer is a separate, narrowly-scoped step**
   (`thenetwork/memory/sanitize.py`) - mandatory Presidio redaction of names, email
   addresses, and phone numbers while keeping organizations and locations for search
   recall, plus an optional higher-fidelity LLM pass that has a fixed prompt and no
   tools. Missing Presidio is a deployment error, not a silent downgrade. The component
   that sees raw cross-user data stays small and auditable; the main agent never
   self-censors.
5. **Capability-style email tools (confused-deputy fix).** `reply_to_sender`
   derives its only recipient from the inbound sender. `send_outreach` accepts an
   opaque `recipient_user_id`; the address is resolved server-side at send time.
   `send_event_recommendation` accepts only a server-bound opaque event id, derives the
   recipient from authenticated context, and composes fixed mail from a sanitized gist.
   None of these tools lets the LLM see or supply a raw address.
6. **Double-opt-in introduction.** Only authenticated consent from both participants lets
   server code send the fixed introductions. Their bodies omit participant names and real
   addresses, print only the server-owned relay address, and use server-resanitized
   proposal gists for the match recap.
7. **Server-only address relay.** After introduction, replies to the stable pair alias are
   authenticated and authorized before any model execution. Server code resolves only the
   other participant and preserves the participant-authored MIME body while replacing
   source routing headers.
8. **Role separation.** The untrusted inbound body is passed as user-role message
   content, never into the system prompt (`thenetwork/agent/core.py`).
9. **Mail-loop prevention (RFC 3834).** Inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*` is skipped; automated agent replies set
   `Auto-Submitted: auto-replied`, while human-to-human relay mail omits it.
10. **Rate limiting / anti-DoS.** Disposable-domain rejection, Postgres-backed inbound and outbound quotas via
    [`limits`](https://limits.readthedocs.io/), a global processed-email quota, and bounded
    Procrastinate worker concurrency cap LLM and email spend. Optional primary-only burst
    detection and a fixed-prompt, no-tools judge use keyed fingerprints and opaque labels;
    only a coordinated-abuse verdict can pause intake.
11. **PII-safe audit correlation.** Opaque trace ids follow each message, while stable
    sender correlation requires an HMAC-derived pseudonym; raw sender addresses are never
    logged.
12. **Credentials.** Never hardcoded - loaded from env / `.env` via
   pydantic-settings.
13. **Optional content scanner.** LlamaFirewall's local Llama Prompt Guard 2 86M
    classifier is opt-in defense-in-depth, never the primary defense. The complete
    capped body is scanned in overlapping, tokenizer-aware windows that include the
    model's special tokens within its 512-token context.
14. **Mutating-tool replay boundary.** Server-side fingerprints prevent a pydantic-ai
    retry from repeating completed database, SMTP, quota, or sent-memory effects within
    one agent run.

The PGP-verified admin channel is separate from the agent-facing SEAL. See
[`docs/security.md`](./docs/security.md) for the complete threat model and contracts.

The red-team suite (`tests/security/`) proves it: adversarial emails must produce
**zero** raw other-person memory text - no names, emails, or bios - in the reply
*or* in any tool argument, even under a fully-hijacked model.

---

## Stack

| Concern | Choice |
|---|---|
| ORM / models | SQLModel over psycopg 3 (`postgresql+psycopg://`) |
| Migrations | Alembic (`CREATE EXTENSION vector` runs idempotently in a migration) |
| Vector store | pgvector - `Vector(1536)`, HNSW cosine |
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
string - no LiteLLM, no proxy glue. Embeddings use OpenAI only: `EMBED_MODEL` must
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

Create a `.env` in the project root. Defaults live in `thenetwork/settings.py`;
the common overrides:

```dotenv
POSTGRES_HOST=localhost   # docker compose overrides this to `db` for the worker
POSTGRES_PORT=5432
POSTGRES_DB=network_db
POSTGRES_USER=network
POSTGRES_PASSWORD=network   # literal password; Settings.database_url percent-encodes it

# LLM - provider is chosen by the model string prefix
AGENT_MODEL=anthropic:claude-sonnet-5
SMALL_AGENT_MODEL=anthropic:claude-haiku-4-5
EMBED_MODEL=text-embedding-3-small  # OpenAI, 1536 dimensions
AGENT_API_KEY=
SMALL_AGENT_API_KEY=
EMBED_API_KEY=

# Mailbox - IMAP (inbound polling) and SMTP (outbound send) are separate
# accounts/credentials, potentially on different providers
IMAP_ACCOUNT=agent@example.com
IMAP_PASSWORD=...
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
# Optional; both use the same IMAP host/port above
RELAY_IMAP_ACCOUNT=relay-inbox@relay.example.com
RELAY_IMAP_PASSWORD=...
SMTP_ACCOUNT=agent@example.com
SMTP_PASSWORD=...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
EMAIL_FROM=agent@example.com
# Dovecot catch-all domain; SES must be allowed to send From this domain
RELAY_DOMAIN=relay.example.com
REQUIRE_SENDER_AUTH=true

# Tuning
WORKER_CONCURRENCY=4
RATE_LIMIT_PER_HOUR=20
UNAUTHENTICATED_RATE_LIMIT_PER_HOUR=6
GLOBAL_EMAIL_RATE_LIMIT_PER_HOUR=200
SENDER_IDENTIFIER_SECRET=long-random-server-secret
PRIMARY_INTAKE_BURST_MONITORING_ENABLED=false
CONTENT_SCAN_ENABLED=false
HF_TOKEN=                 # first enabled startup only, until the model is cached
```

### 2. Install

```bash
uv pip install -e .
```

The required `en_core_web_lg` spaCy model is installed with the project; no
separate model download is needed.

The project installs pinned LlamaFirewall scanner dependencies and uses
`meta-llama/Llama-Prompt-Guard-2-86M`, a gated model under the Llama 4 Community
License. Accept that license on Hugging Face and set `HF_TOKEN` before the first
enabled worker startup. `CONTENT_SCAN_ENABLED` is the only feature switch: when
enabled, startup downloads and initializes the model before queue processing;
later starts use `HF_HOME` and need no token. LlamaFirewall brings PyTorch and
its platform wheels, so allow materially more image/disk space and memory even
when scanning is disabled at runtime.

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

This service needs **no application-owned inbound network access** - it polls IMAP (outbound),
pulls jobs from local Postgres, and calls LLM/SMTP APIs (outbound). So there's
no web server, reverse proxy, or public port to expose. A single small VPS with
SSH access is enough. Hidden-address replies arrive through the existing
Dovecot catch-all mailbox, not an application endpoint.

### Single-VPS Docker Compose

`docker-compose.yml` runs two services: `db` (pgvector Postgres, bound to
`127.0.0.1` only, state in the `pgdata` volume) and `worker`. Postgres on the
box is fine at this scale.

```bash
cp .env.example .env          # fill in secrets
docker compose up -d --build  # builds the image, starts db + worker
docker compose logs -f worker
```

To enable scanning, set `CONTENT_SCAN_ENABLED=true`. On the first enabled start,
provide `HF_TOKEN`; compose persists the downloaded Prompt Guard 2 model in the
`hf-cache` volume at `HF_HOME`. The worker refuses to start if scanning is enabled
without either that cache or a non-interactive token, and it finishes model
initialization before accepting jobs. Scanner-disabled deployments load no
LlamaFirewall code and require neither model weights nor Hugging Face credentials.

The container entrypoint runs `alembic upgrade head` before starting, so
migrations apply automatically on every deploy.

### Safe redeploys (no lost or half-processed jobs)

Procrastinate makes this safe by design - durable job rows in Postgres,
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
uv run pytest                       # full suite
uv run pytest -m "not integration"  # skip tests that need a live pgvector DB
```

- `tests/security/` - the SEAL red-team and security contracts
- `tests/scenarios/` - emergent-behavior evals (pydantic-evals)
- `tests/test_match_pipeline.py` - semantic match / search pipeline

---

## Layout

```
thenetwork/
  admin/      PGP/MIME-verified operator commands
  agent/      pydantic-ai agent: core wiring, tools, prompts, deps
  db/         SQLModel models + session
  email/      IMAP intake, SMTP output, hidden-address relay, templates
  embed/      OpenAI embedding wrapper (1536 dimensions)
  memory/     the SEAL: sanitize (gist) + seal (self/other gate)
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
