# Design decisions & rejected approaches

The *why* behind the architecture - read this before proposing a "cleaner" schema or a
new abstraction, because most obvious-looking alternatives were considered and rejected
on purpose.

## Guiding principle

**Use mainstream, well-adopted open-source frameworks and prior art. Do not reinvent
solved problems. Do not depend on flimsy low-star libraries.** Hand-write only the
genuine domain glue; for everything else (queues, rate limiting, ORM, migrations,
embeddings, graph math, mail) use the established solution - sometimes that's just the
Python stdlib.

The genuine glue here is exactly one thing: **memory + the privacy seal over freeform
memory** (`thenetwork/memory/`, the SEAL - see @docs/security.md). Everything else is a
thin wrapper over a well-adopted library, swappable by config.

## Design judgment calls

- **Freeform `memories.text` is the substrate, not a schema.** Meaning (is this an
  attribute? an intro? an event?) is drawn by the agent from the text at reasoning time,
  never enforced by columns. Adding a `kind`/`status`/`category` column is the thing this
  whole design exists to avoid.
- **Small append-only chunks, not one blob per person.** Small chunks embed cleanly; a
  big per-person notepad smears semantic recall.
- **Build `refs` plumbing day one; defer proximity *scoring* until the graph is dense
  enough to earn it.** The graph edges exist from the start (any 2+-ref memory), but
  Jaccard scoring only becomes meaningful once there's real connection density.
- **The graph is a projection, never a table.** "Who knows whom" is derived from
  multi-ref memories at query time; there is no curated edge table to keep in sync.
- **Behaviors are emergent, not scripted.** Onboarding, matchmaking, introductions, and
  one-way FYIs come from system-prompt guidance plus `pydantic-evals` cases asserting
  *reasonable* behavior - no branching control flow, no scenario `if user_record IS NULL`.
- **Inbound body extraction leans on imap-tools + BeautifulSoup, not hand-rolled MIME
  walking.** Strict attachment-boundary pruning was deliberately relaxed: `MAX_BODY_CHARS`
  truncation is the real size guard regardless of how the body was assembled, and the
  inbound body is untrusted content either way - the SEAL governs what can leave the
  system, not what can enter it. Chasing exact attachment-subtree exclusion added
  bespoke parsing code for a property the size cap and the SEAL already cover.
- **Presidio is mandatory for deterministic gist sanitization.** Names, email addresses,
  and phone numbers are redacted by a mainstream PII library, with the spaCy model baked
  into the Docker image at build time and checked at worker startup. Organizations and
  locations stay in gists because those gists are embedded for company/place recall; the
  opt-in fixed-prompt LLM tier handles quasi-identifying combinations.
- **Tools report expected refusals as structured status results.** Policy and
  world-state outcomes are not exceptions for the model to retry; pydantic-ai
  gets one retry solely for argument validation. This keeps rate limits,
  suppression, ownership checks, and exhausted caps explicit and bounded.
- **Mutating tool replay is scoped to one model run and keyed server-side.** Pydantic retry
  prompts can cause a model to repeat already-completed calls, so the dependency container
  caches completed results by canonical validated arguments and occurrence. Do not accept a
  model-supplied idempotency key, and do not turn this narrow retry boundary into a general
  domain operation table.
- **Events are a narrow operational exception to the freeform-only substrate.** Event
  meaning remains freeform, but recommendations require server-enforced stable identity,
  ownership, expiry/cancellation, delivery deduplication, and an event-only opt-out. The
  `events`, `event_recommendations`, and `event_suppressions` tables store only that
  lifecycle state. They do not model categories, RSVPs, attendance, reminders, or
  calendars, and they are not memories, introduction consent, or proactive pair state.

## Explicitly rejected (anti-patterns)

Each of these was deliberately chosen *against*. Don't reintroduce one without
understanding why it was dropped.

- ❌ Domain columns (`skills[]`, `available_to_collaborate`, `intent_*`) → freeform `memories.text`.
- ❌ General typed object tables (`items`, `subscriptions`) or
  `kind`/`direction`/`status` discriminators → meaning lives in text + agent reasoning.
  The narrowly scoped event lifecycle tables described above are the sole exception.
- ❌ A curated `network_connections` edge table → graph projected from multi-ref memories.
- ❌ One big notepad blob per person → small append-only chunks (clean recall).
- ❌ In-place memory edits → delete + create (keeps embeddings/gists consistent).
- ❌ Withhold-a-column privacy → two-layer raw/gist with a dedicated sanitizer.
- ❌ LLM computing graph proximity at query time → NetworkX over projected edges.
- ❌ Raw `psycopg2` + hand-written SQL strings → SQLModel + pgvector.
- ❌ `MIMEMultipart` → stdlib `EmailMessage`.
- ❌ `while True: ... time.sleep()` daemon → Procrastinate worker.
- ❌ IMAP seen-flag as the unit of durability → Postgres job row.
- ❌ Scenario branching (`if user_record IS NULL`) → emergent behavior + evals.
- ❌ Tools returning other users' names/emails/bios → minimal disclosure (gist only).
- ❌ LLM handling raw email addresses → capability tool, server-side resolution.
- ❌ Regex-only sanitizer fallback when Presidio is unavailable → fail fast; a silent
  downgrade can produce cross-user gists with raw names.
- ❌ LiteLLM / proxy glue → pydantic-ai native multi-provider.
- ❌ `np.random.rand(1536)` placeholder embeddings → OpenAI embedding wrapper constrained to 1536 dimensions.
- ❌ Bespoke rate limiting → `limits`.
- ❌ Introduction digest batching → skip capped proactive candidates and retry them on a
  later sweep; a person's declines remain the meaningful rate limiter.
- ❌ Heavyweight guardrail frameworks (NeMo, Guardrails-AI) → architectural least-privilege + RFC 3834 + optional scanner.
