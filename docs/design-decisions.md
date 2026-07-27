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
  bespoke parsing code for a property the size cap and the SEAL already cover. Ordinary
  agent processing still never reads attachments. It uses imap-tools' parsed attachment
  metadata only to derive a bounded non-inline count that crosses the queue and enters
  agent context. The signal is count-only because filenames, MIME types, and other
  attachment metadata are attacker-authored strings; only the server-derived integer is
  trusted enough to tell the agent that unread content exists. This does not reinstate
  attachment-subtree parsing, and the original message remains in INBOX. The HTML
  fallback does preserve descriptive anchor targets as bounded HTTP(S) text, with
  per-URL and per-message limits, because losing the referent changes what the sender
  said. This does not add link fetching or make a URL's text evidence for what is behind
  it. URLs are deliberately not redacted from raw memory text; the sanitizer still
  replaces identifying profile handles in cross-user gists while generic project URLs
  may remain useful recall text.
- **One mandatory local span classifier does all gist sanitization.** `SANITIZE_MODEL`
  (`openai/privacy-filter`) labels spans; the allow-list in `memory/sanitize.py` decides
  which become bracket tokens. It replaced a three-part stack - a Presidio/spaCy NER
  pass, a hand-written structural pass over platform handles, and an opt-in per-write
  LLM tier - because one purpose-built model beat all three on the cases that motivated
  them. Measured before adoption: it catches `mkly`, `@atlas`, common-noun given names
  (Rose/Mark/Bill), non-Western and Cyrillic names, and obfuscated addresses
  (`mike [at] mkly [dot] io`) that the regex tier never saw, and it stops tagging
  `LinkedIn` as a person. It costs ~2 GB of local weights and one forward pass, no
  credential and no network call, and it removes `presidio-analyzer`, `spacy`,
  `en_core_web_lg`, and a per-write model call from the write path.
- **Response-log redaction runs the same classifier, not its own recognizer stack.**
  `security/log_redaction.py` used to be Presidio plus eight hand-written regexes for
  intro tokens, UUIDs, `user_`/`request_`/`trace_` prefixes, and provider key shapes.
  Measured against the classifier, those patterns were mostly redundant: it labels
  `sk-ant-...`, `sk_live_...`, `password:` values, `api_key=` assignments, `trace_id=`
  and `user_...` identifiers, URLs, emails, names, and phone numbers on its own. Both
  callers now go through `sanitize.classify_spans`, so the 2.7 GB of weights load once
  per process instead of twice, and the last reason to depend on `presidio-analyzer`
  is gone. The allow-lists stay separate: logs also redact `private_date`, which gists
  keep. Two measured gaps - an AWS `AKIA...` key and a bare `[intro:<uuid>]` token -
  now pass through, which is acceptable because log redaction is defense in depth for
  diagnostics, not a boundary the SEAL rests on; the SEAL's boundary is
  `memory/sanitize.py` and the search projections.
- **Sanitization has no off switch.** There is no `SANITIZE_*_ENABLED` setting and no
  fallback path. A sanitizer that can be disabled or that degrades silently is a
  sanitizer you cannot reason about at the SEAL boundary, and the failure mode is a
  cross-user gist carrying a raw name.
- **The classifier's coverage is probabilistic, and the answer to that is a later
  sweep, not a regex backstop.** Adding pattern matching under the model reintroduces
  exactly the brittle, word-list-dependent code this replaced, in exchange for cases the
  model already handles better. The planned periodic large-model pass over stored gists
  is where residue gets cleaned. That sweep rewrites gists in place, so it must follow
  `scripts/redact-memory.sh`'s pattern (preserve memory id, `refs`, and `created_at`;
  refresh the gist; recompute the embedding), which is the sole sanctioned exception to
  forget-plus-remember.
- **`private_date` is deliberately not redacted.** "A Rust meetup Thursday" is the
  perishability and recall signal a gist exists to carry, and `created_at` already
  carries recency. Organizations and place names have no label in the taxonomy at all,
  which happens to match the long-standing decision to keep them for company/place
  recall. `private_url` *is* redacted, unlike the pattern tier it replaces: a profile
  URL is a handle, and a handle names a real person outside this system.
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
- ❌ In-place memory edits → delete + create (keeps embeddings/gists consistent). The only exception is the operator script `scripts/redact-memory.sh`, where `forget` + `remember` cannot be used because the memory ID, `refs`, and `created_at` timestamp must survive an operator PII redaction; this rule still strictly binds every agent write path.
- ❌ Withhold-a-column privacy → two-layer raw/gist with a dedicated sanitizer.
- ❌ LLM computing graph proximity at query time → NetworkX over projected edges.
- ❌ Raw `psycopg2` + hand-written SQL strings → SQLModel + pgvector.
- ❌ `MIMEMultipart` → stdlib `EmailMessage`.
- ❌ `while True: ... time.sleep()` daemon → Procrastinate worker.
- ❌ IMAP seen-flag as the unit of durability → Postgres job row.
- ❌ Scenario branching (`if user_record IS NULL`) → emergent behavior + evals.
- ❌ Tools returning other users' names/emails/bios → minimal disclosure (gist only).
- ❌ LLM handling raw email addresses → capability tool, server-side resolution.
- ❌ Any sanitizer fallback or downgrade path → fail fast; a silent downgrade can
  produce cross-user gists with raw names.
- ❌ Regex/word-list handle matching under the classifier → the model already catches
  handles, dictionary-word handles, and obfuscated addresses the patterns missed.
- ❌ A per-write LLM sanitizer tier → one local forward pass, plus a periodic
  large-model sweep over stored gists for what the classifier missed.
- ❌ Presidio + a custom regex recognizer list in the response-log redactor → the same
  classifier, one loaded copy, one shared span function, its own allow-list.
- ❌ LiteLLM / proxy glue → pydantic-ai native multi-provider.
- ❌ `np.random.rand(1536)` placeholder embeddings → OpenAI embedding wrapper constrained to 1536 dimensions.
- ❌ Bespoke rate limiting → `limits`.
- ❌ Introduction digest batching → skip capped proactive candidates and retry them on a
  later sweep; a person's declines remain the meaningful rate limiter.
- ❌ Heavyweight guardrail frameworks (NeMo, Guardrails-AI) → architectural least-privilege + RFC 3834 + optional scanner.
