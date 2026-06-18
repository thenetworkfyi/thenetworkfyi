# The Network — Implementation Plan

An email-driven agentic connection engine. Inbound emails are polled over IMAP,
durably queued, and handed to an LLM ReAct agent. The agent's substrate is **not
a networking schema** — it is a store of **freeform memories** the agent reads,
writes, and searches. The agent routes relevance: it may introduce two people,
share an event with people who'd care, capture a fact about someone, or do
nothing. Runs on a single VPS.

This document is the authoritative companion to the Taskwarrior breakdown
(`project:thenetwork.*`). The tasks hold the *what* and the *order*; this holds
the *why* and the cross-cutting contracts.

**Initial framing:** tech workers (the wedge), but the substrate carries zero
domain assumptions — events, activities, and buy/sell are new *agent behaviors
over memory*, not schema migrations.

---

## Guiding principle

**Use mainstream, well-adopted open-source frameworks and prior art. Do not
reinvent solved problems. Do not depend on flimsy low-star libraries.**
Hand-write only the genuine domain glue. For everything else (queues, rate
limiting, ORM, migrations, embeddings, graph math, mail) use the established
solution — sometimes that's just the Python stdlib.

The genuine glue here is **memory + the privacy seal over freeform memory** —
everything else stays as described below.

---

## Settled stack

| Concern | Choice | Notes |
|---|---|---|
| ORM | **SQLModel** | over psycopg 3 |
| Migrations | **Alembic** | extension creation lives in a migration |
| DB driver | **psycopg 3** | `postgresql+psycopg://` |
| Vector store | **pgvector** | `Vector(1536)`, HNSW cosine, direct SQL queries |
| Agent | **pydantic-ai** (native, multi-provider) | provider swapped by config string — **no LiteLLM**, no proxy |
| Embeddings | `embed_text` via provider-agnostic wrapper | provider-swappable via `EMBED_MODEL` |
| Graph proximity | **NetworkX** | `common_neighbors` / `jaccard_coefficient` over projected graph |
| Settings | **pydantic-settings** `BaseSettings` | env / `.env` |
| IMAP | **imap-tools** | auto-decoded body + sender, mark seen |
| SMTP | stdlib `email.message.EmailMessage` + `smtplib` | modern API, **NOT** MIMEMultipart |
| Job queue | **Procrastinate** | Postgres-native durable queue; LISTEN/NOTIFY + SKIP LOCKED, no broker/Redis |
| Rate limiting | **limits** | the Flask-Limiter engine, backed by our Postgres; no bespoke token bucket |
| Tests | **pytest** + **pydantic-evals** | `TestModel`/`FunctionModel` for deterministic agent runs |
| Content scan (optional) | LLM Guard / provider moderation | opt-in defense-in-depth, **not** NeMo / Guardrails-AI |

**Local dev:** docker-compose `pgvector/pgvector:pg17`.
**Production:** Neon managed Postgres via `DATABASE_URL`.
`CREATE EXTENSION vector` runs idempotently in the Alembic migration.

Vendor-agnosticism comes from pydantic-ai (agent) and the embedding wrapper both
being multi-provider, selected by `AGENT_MODEL` / `EMBED_MODEL` config strings.
No glue package, no proxy.

---

## Runtime architecture

```
[Inbox] --IMAP--> Producer (imap-tools poll)
                      | enqueue ONE Procrastinate job per message, THEN mark seen
                      v
              [Postgres job row]  <- source of truth (durable)
                      | LISTEN/NOTIFY + SKIP LOCKED
                      v
                  Worker (Procrastinate task)
                      | runs the pydantic-ai agent: Think / Act / Observe
                      | tools: remember / forget / search / dispatch_email
                      v
                  Reply --SMTP--> [Sender]
```

- **Durability lives in the Postgres job row, not the IMAP seen-flag.** The
  seen-flag is kept only as cheap dedup. Crash mid-run → Procrastinate retries,
  nothing is lost.
- **Retries/backoff** are Procrastinate's job, not hand-rolled.
- **Process supervision** (systemd `Restart=always`) is deferred with the deploy
  category — re-home it there when deploy returns.

---

## Data model

Two tables. That is the whole durable model.

### `people`
Pure identity + addressing + the security boundary.

- `id` (uuid str, pk)
- `email` (unique, indexed) — the mailer resolves this server-side
- `name`

Nothing about *why* a person is here lives on this row. That is all memory.

### `memories`
A growing pile of freeform chunks the agent owns.

- `id` (uuid str, pk)
- `text` — freeform; the source of truth
- `embedding` `Vector(1536)` — HNSW cosine
- `refs` — set of `people.id` this memory concerns (**0..N**)
- `gist` — PII-stripped summary produced by the sanitizer; cross-user eligible
- `created_at` — perishability / recency signal

`refs` cardinality is the only structure, and it is incidental:

- **0 refs** → general knowledge / agent notes (e.g. "a Rust meetup Thursday")
- **1 ref** → an attribute about a person ("just moved to Berlin", "likes dogs")
- **2+ refs** → *also* contributes an undirected graph edge between those people

No `kind`, no `direction`, no `status`, no `category`, no `tags` columns — those
are distinctions the agent draws from `text` at reasoning time, not facts the DB
enforces. Chunks are small (they embed cleanly; one big per-person blob smears
recall).

### The graph is a projection, not a table

`network_connections` is removed. "Who knows whom" is derived: nodes = people,
an edge exists between two people because some memory references both, edge weight
= count / recency of shared memories. The agent does the language→reference
mapping at write time; NetworkX does the multi-hop proximity math at query time
(the LLM is bad at that). Undirected projection for the warm-intro signal;
directional nuance ("mentor → mentee") stays in the memory text.

**Build `refs` plumbing day one; defer proximity *scoring* until the graph is
dense enough to earn it.**

---

## Agent surface

Memory is CRUD, exposed as tools; everything else is emergent behavior.

- `remember(text, refs)` — write a chunk.
- `forget(memory_id)` — delete a chunk. **Edit = forget + remember** (keeps the
  embedding always consistent with the text; no stale vectors).
- `search(query) -> [ {person_id, gist, similarity} ]` — semantic recall.
  Returns opaque ids + **non-identifying gist only** for other people (see seal).
- `dispatch_email(person_id, subject, body, …)` — opaque id in, real address
  resolved server-side (capability / confused-deputy fix).

Behaviors — onboarding, attribute capture, introductions, one-way event shares /
FYIs, broadcasts/digests — are **emergent**: system-prompt guidance +
`pydantic-evals` cases asserting reasonable behavior, **no branching control
flow**, no scenario scripts. Outcomes range from a full double-blind introduction
(creates a relationship memory → an edge) down to a one-way share (no edge, no
handshake).

---

## Security model — the SEAL over freeform memory

The critical concern: **prompt injection must not be able to exfiltrate user
identities or data, yet the agent must still email people on a user's behalf.**
We make leakage *structurally impossible* rather than prompt-dependent.

In the old schema, minimal-disclosure was trivial: other people's data sat in
typed columns you could withhold. In a freeform store, a memory like *"Bob
(bob@x.com) is a Rust dev looking for a cofounder"* is **raw PII inside `text`**
— retrieving and returning it for anyone but Bob lets a prompt-injection
exfiltrate it. The seal can no longer be "withhold a column."

Structural defense (leakage stays *impossible*, not prompt-dependent):

1. **Two-layer memory for person-referencing chunks.** Each carries a **raw
   form** (retrievable only for that person's own requests) and a **sanitized
   gist** (PII-stripped) that is the *only* thing cross-user search may return.
2. **Cross-user retrieval and the LLM only ever touch the gist + opaque ids.** A
   hijacked model has no identifying text to leak. Real addresses never enter LLM
   context — the mailer resolves them server-side.
3. **Self/other gate:** sole-ref-is-sender → raw text; otherwise → gist only.
4. **Sanitizer is a separate, narrowly-scoped step**, *not* the main
   untrusted-input-influenced agent self-censoring. A dedicated sanitization pass
   (and/or deterministic PII strip) produces the gist, so the component that sees
   raw cross-user data is small and auditable.
5. **Capability-style email tool (confused-deputy fix).**
   `dispatch_email(recipient_user_id, ...)` takes opaque internal IDs; the mailer
   resolves the real address server-side at send time. The LLM never sees or
   supplies a raw address.
6. **Role separation.** The untrusted inbound email body is passed as user-role
   message content, **never** concatenated into the system prompt.
7. **Mail-loop prevention (RFC 3834).** Skip inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*`; set `Auto-Submitted: auto-replied` on all
   outbound.
8. **Rate limiting / anti-DoS.** Per-sender quota via `limits` (Postgres-backed)
   + bounded Procrastinate worker concurrency as the global LLM-spend ceiling.
9. **Credentials.** Never hardcoded — loaded from env / `.env` via
   pydantic-settings. Production DB is Neon via `DATABASE_URL` secret.
10. **Optional content scanner.** LLM Guard / provider moderation as opt-in
    defense-in-depth — never the primary defense.

The red-team test ("THE SEAL") proves it: adversarial emails must produce
**zero** raw other-person memory text — no names, emails, or bios — in the reply
*or* in any tool argument, even under a fully-hijacked model.

---

## Scenarios are emergent, not a ruleset

Onboarding, matchmaking, and introductions are **emergent agent behavior** —
system-prompt guidance + a `pydantic-evals` case asserting *reasonable* behavior
(sensible tool-call pattern + output), **not** branching control flow and not
exact scripts.

---

## Build order (waves)

| Wave | Focus | Gate |
|---|---|---|
| 1 | Schema: `people` + `memories(text, embedding, refs, created_at)`; rewrite migration | migration |
| 2 | Memory layer + SEAL: two-layer raw/gist + sanitization pass | the seal |
| 3 | Agent tools: `remember`/`forget`/`search`; drop profile/skill tools; mailer unchanged | tools |
| 4 | Search: memory semantic search; graph projection from multi-ref memories (scoring deferred) | search |
| 5 | Prompt/copy: neutral connector framing, tech-worker launch voice | prompt |
| 6 | Tests: rework fixtures; rewrite red-team SEAL for freeform memory | — |
| 7 | Docs: fold `NEW_PLAN.md` into `PLAN.md`; retire old data/security sections | — |

Tasks are wired with Taskwarrior `depends` so `task ready` reveals one wave at a
time. Within a wave, pick order freely. Each gate is the anchor that unlocks the
next wave.

---

## Explicitly rejected (anti-patterns)

- ❌ domain columns (`skills[]`, `available_to_collaborate`, `intent_*`) →
  freeform `memories.text`
- ❌ typed object tables (`events`, `items`, `subscriptions`) / `kind` /
  `direction` / `status` discriminators → meaning lives in text + agent reasoning
- ❌ a curated `network_connections` edge table → graph projected from multi-ref
  memories
- ❌ one big notepad blob per person → small append-only chunks (clean recall)
- ❌ in-place memory edits → delete + create
- ❌ withhold-a-column privacy → two-layer raw/gist with a dedicated sanitizer
- ❌ LLM computing graph proximity at query time → NetworkX over projected edges
- ❌ raw `psycopg2` + hand-written SQL strings → SQLModel + pgvector
- ❌ `MIMEMultipart` → stdlib `EmailMessage`
- ❌ `while True: ... time.sleep()` daemon → Procrastinate worker under systemd
- ❌ IMAP seen-flag as the unit of durability → Postgres job row
- ❌ scenario branching (`if user_record IS NULL`) → emergent behavior + evals
- ❌ tools returning other users' names/emails/bios → minimal disclosure (gist only)
- ❌ LLM handling raw email addresses → capability tool, server-side resolution
- ❌ LiteLLM / proxy glue → pydantic-ai native multi-provider
- ❌ `np.random.rand(1536)` placeholder embeddings → provider-agnostic embed wrapper
- ❌ bespoke rate limiting → `limits`
- ❌ heavyweight guardrail frameworks (NeMo, Guardrails-AI) → architectural
  least-privilege + RFC 3834 + optional scanner
