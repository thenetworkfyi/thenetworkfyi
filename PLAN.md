# The Network — Implementation Plan

An email-driven agentic professional-networking / matchmaking engine. Inbound
emails are polled over IMAP, durably queued, and handed to an LLM ReAct agent
that matches people via semantic search + network-graph proximity, updates
Postgres, and replies over SMTP. Runs on a single VPS.

This document is the authoritative companion to the Taskwarrior breakdown
(`project:thenetwork.*`). The tasks hold the *what* and the *order*; this holds
the *why* and the cross-cutting contracts.

---

## Guiding principle

**Use mainstream, well-adopted open-source frameworks and prior art. Do not
reinvent solved problems. Do not depend on flimsy low-star libraries.**
Hand-write only the genuine domain glue. For everything else (queues, rate
limiting, ORM, migrations, embeddings, graph math, mail parsing) use the
established solution — sometimes that's just the Python stdlib.

---

## Settled stack

| Concern | Choice | Notes |
|---|---|---|
| ORM | **SQLModel** | over psycopg 3 |
| Migrations | **Alembic** | extension creation lives in a migration |
| DB driver | **psycopg 3** | `postgresql+psycopg://` |
| Vector store | **pgvector** | `Vector(1536)`, HNSW cosine, GIN on `skills[]` |
| Agent | **pydantic-ai** (native, multi-provider) | provider swapped by config string — **no LiteLLM**, no proxy |
| Embeddings | **LlamaIndex** `OpenAIEmbedding` | same framework as the retriever; provider-swappable |
| Retrieval | **LlamaIndex** `PGVectorStore` retriever + reranker | not raw SQL |
| Graph proximity | **NetworkX** | `common_neighbors` / `jaccard_coefficient` |
| Settings | **pydantic-settings** `BaseSettings` | env / `.env` |
| IMAP | **imap-tools** | auto-decoded body + sender, mark seen |
| SMTP | stdlib `email.message.EmailMessage` + `smtplib` | modern API, **NOT** MIMEMultipart |
| Job queue | **Procrastinate** | Postgres-native durable queue; LISTEN/NOTIFY + SKIP LOCKED, no broker/Redis |
| Rate limiting | **limits** | the Flask-Limiter engine, backed by our Postgres; no bespoke token bucket |
| Tests | **pytest** + **pydantic-evals** | `TestModel`/`FunctionModel` for deterministic agent runs |
| Content scan (optional) | LLM Guard / provider moderation | opt-in defense-in-depth, **not** NeMo / Guardrails-AI |

**Local dev:** docker-compose `pgvector/pgvector:pg17`.
**Production:** Neon managed Postgres via `DATABASE_URL`.
`CREATE EXTENSION vector + pg_trgm` runs idempotently in the Alembic migration.

Vendor-agnosticism comes from pydantic-ai (agent) and LlamaIndex (embeddings)
both being multi-provider, selected by `AGENT_MODEL` / `EMBED_MODEL` config
strings. No glue package, no proxy.

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
                      | tools: inspect / save_or_update / match_candidates / dispatch_email
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

- **Profile** — `name`, `email`, `bio`, `skills TEXT[]`, `intent_description`,
  `available_to_collaborate`, dual vectors `identity_vector` (who they are) and
  `intent_vector` (what they want), both `Vector(1536)`.
- **NetworkConnection** — self-referencing edge with `connection_strength`,
  composite PK, cascade delete.
- **Open decision:** symmetric (one row) vs directed (two rows) edges — this
  choice drives the NetworkX graph build (task `db/6`).

---

## Security model (the seal)

The critical concern: **prompt injection must not be able to exfiltrate user
identities or data, yet the agent must still email people on a user's behalf.**
We make leakage *structurally impossible* rather than prompt-dependent.

1. **Minimal disclosure (anti-exfiltration).** Lookups for anyone other than the
   sender return opaque IDs + non-identifying rationale (skill/intent overlap,
   similarity, mutual-connection count) **only** — never names, emails, or raw
   bios into LLM context. Full data is returned only for the sender's *own*
   profile. A hijacked model has no PII to steal.
2. **Capability-style email tool (confused-deputy fix).**
   `dispatch_email(recipient_user_id, ...)` takes opaque internal IDs; the mailer
   resolves the real address server-side at send time. The LLM never sees or
   supplies a raw address, so it cannot redirect mail or echo addresses.
3. **Double-blind, consent-gated introductions.** The agent introduces by
   emailing each party *by ID*; identities/contacts are exchanged only after
   **both** opt in. Matching reasoning therefore runs on pseudonymized data
   ("a founder in your space") until consent — this is correct privacy-preserving
   behavior, not a limitation.
4. **Role separation.** The untrusted inbound email body is passed as user-role
   message content, **never** concatenated into the system prompt.
5. **Mail-loop prevention (RFC 3834).** Skip inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*` (self-send is one case); set
   `Auto-Submitted: auto-replied` on all outbound.
6. **Rate limiting / anti-DoS.** Per-sender quota via `limits` (Postgres-backed)
   + bounded Procrastinate worker concurrency as the global LLM-spend ceiling
   regardless of inbound flood. No bespoke token bucket.
7. **Credentials.** Never hardcoded — loaded from env / `.env` / app passwords
   via pydantic-settings. Production DB is Neon via `DATABASE_URL` secret.
8. **Optional content scanner.** LLM Guard / provider moderation as opt-in
   defense-in-depth — never the primary defense.

The red-team test ("THE SEAL", `testing/41`) proves it: adversarial emails must
produce **zero** other-user PII in the reply *or* in any tool argument, even
under a fully-hijacked model.

---

## Scenarios are emergent, not a ruleset

Onboarding, matchmaking, and double-introductions are **emergent agent behavior**
— system-prompt guidance + a `pydantic-evals` case asserting *reasonable*
behavior (sensible tool-call pattern + output), **not** branching control flow and
not exact scripts. The only genuinely new code is the **proactive outreach
trigger** (`scenarios/23`): a periodic Procrastinate task that scans for
opportunities and *enqueues* an agent job — it only triggers; whether and how to
introduce stays the agent's decision.

---

## Build order (coarse phase gates)

Tasks are wired with Taskwarrior `depends` so `task ready` reveals one wave at a
time. Within a wave, pick order freely. Each gate is the anchor that unlocks the
next wave.

| Wave | Focus | Tasks | Gate (anchor) |
|---|---|---|---|
| 0 | Foundation | infra 1,2,3,4 | **4** settings |
| 1 | Persistence | db 5,6,32,33; security 27 | **33** migration |
| 2 | Capabilities + search | embeddings 7,8,9; email 12,13; search 10,11 | **11** match assembly |
| 3 | Agent core | agent 14–19; security 35,36,38 | **19** system prompt |
| 4 | Runtime | agent 20,34; security 25,26,28,37,39 | **34** worker |
| 5 | Scenarios | scenarios 21,22,23,24 | **24** double-intro |
| 6 | Testing | testing 29,30,31,40,41,42 | — terminal |

Intra-wave dependencies are intentionally loose (e.g. search 11 isn't hard-blocked
on embeddings 8). Tighten a specific wave later if needed without touching the
gate structure.

---

## Explicitly rejected (anti-patterns)

These appeared in the original blueprint and were deliberately discarded:

- ❌ raw `psycopg2` + hand-written SQL strings → SQLModel + LlamaIndex retriever
- ❌ `MIMEMultipart` → stdlib `EmailMessage`
- ❌ `while True: ... time.sleep()` daemon → Procrastinate worker under systemd
- ❌ IMAP seen-flag as the unit of durability → Postgres job row
- ❌ scenario branching (`if user_record IS NULL`) → emergent behavior + evals
- ❌ tools returning other users' names/emails/bios → minimal disclosure
- ❌ LLM handling raw email addresses → capability tool, server-side resolution
- ❌ LiteLLM / proxy glue → pydantic-ai native multi-provider
- ❌ `np.random.rand(1536)` placeholder embeddings → LlamaIndex embeddings
- ❌ bespoke rate limiting → `limits`
- ❌ heavyweight guardrail frameworks (NeMo, Guardrails-AI) → architectural
  least-privilege + RFC 3834 + optional scanner
