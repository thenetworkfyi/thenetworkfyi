# Architecture

The "big picture" that requires reading several files to reconstruct. See
@docs/design-decisions.md for the rationale behind each choice.

## Message flow

```
[Inbox] --IMAP--> Producer (imap-tools poll)
                    | optional sealed burst observation / primary-only pause
                    | enqueue ONE Procrastinate job per message, THEN mark seen
                    v
            [Postgres job row]  <- durable source of truth
                    | LISTEN/NOTIFY + SKIP LOCKED
                    v
                Worker (Procrastinate task: process_email)
                    | rate-limit + optional content scan
                    | run the pydantic-ai agent: Think / Act / Observe
                    | tools: person memory + introductions + event lifecycle /
                    |        sealed one-way event recommendations
                    v
                Reply --SMTP--> [Sender]
                    | append to IMAP Sent folder (best-effort, post-send)
                    v
              [Sent folder]

[hidden-<pair-token>@RELAY_DOMAIN]
                    | Dovecot catch-all -> configured IMAP inbox/job
                    v
          Worker pair resolver --SES/SMTP--> [Opposite participant]

[sealed primary observations] --hourly fixed-policy judge--> [primary pause state]
```

There is **one** long-lived process (`thenetwork-worker` → `worker/tasks.py:main`). It
drains the Procrastinate queue *and*, via periodic tasks, polls IMAP every minute
(`producer.poll_inbox`), runs three independent hourly discovery scans (graph and semantic
people matching plus semantic event recommendations), and runs the primary-inbox abuse
judge at minute 15. No separate producer daemon is required; `thenetwork-producer` is just
a manual one-shot poll for cron/debugging.

- **Producer** (`worker/producer.py`): polls the primary and, when configured, separate
  relay IMAP inboxes for unseen mail, enqueues exactly one durable job per message, and
  marks each message seen in its source mailbox *only after* enqueue. Durability lives
  in the Postgres job row, not the IMAP seen-flag - a mid-run crash means the job retries
  (`max_attempts=3`) and nothing is lost. The producer only ever flips `\Seen`; it never
  deletes or moves inbound mail, so everything each account has received stays in INBOX
  permanently, the same as a normal mailbox.
- **Primary abuse controls** (`email/intake_observations.py`,
  `worker/abuse_judge.py`): disposable sender domains are rejected before enqueue. When
  monitoring is enabled, ordinary primary messages are recorded as keyed sender, domain,
  and body fingerprints before enqueue; a 25-distinct-new-sender rolling-hour burst pauses
  the primary batch before it can be marked seen. The hourly fixed-policy judge sees only a
  bounded, sender-diverse 24-hour sample with run-local opaque labels. A strict
  `coordinated_abuse` verdict can only pause primary intake and increment a bounded system
  control metric that Alertmanager owns. It has no tools, raw content, identities, relay
  input, direct mail path, or resume capability. While paused,
  ordinary primary messages remain unread; verified admin and relay candidates still run.
- **Inbound body extraction** (`email/inbound.py`): prefers imap-tools'
  `MailMessage.text`, falling back to `MailMessage.html` run through BeautifulSoup to
  recover visible text when a message has no plain-text part. Descriptive HTML anchors
  retain a bounded HTTP(S) href suffix: each rendered URL is at most 120 characters,
  query and fragment data is dropped when truncation is needed, duplicate destinations
  are omitted, and at most 20 distinct links are added. Other schemes are not rendered,
  and this is extraction fidelity only - the agent does not fetch link destinations. No
  hand-rolled MIME walking or attachment traversal - imap-tools has already done that. Attachments are
  never read on the ordinary agent path. Intake derives a bounded count of non-inline
  attachments from imap-tools' parsed metadata and carries only that integer through the
  durable job into agent context, so the agent can accurately ask the sender to paste
  relevant content. Filenames, MIME types, and other attachment-authored strings do not
  cross that boundary. The extracted body is bounded by `MAX_BODY_CHARS`, which is the
  size guard for downstream scanners and model context; a message whose decoded body
  exceeds the hard reject limit is flagged rather than truncated silently.
- **Worker** (`worker/tasks.py`): Postgres-native Procrastinate (LISTEN/NOTIFY +
  `SKIP LOCKED`, no Redis/broker). Enforces per-sender rate limit and optional content
  scan, resolves whether the sender is a known `Person`, then calls
  `agent/core.py:run_agent_for_email`. Worker concurrency is the global LLM-spend ceiling.
- **Daily token budget** (`security/token_budget.py`, see @docs/development.md): a
  rolling-24h `DAILY_AGENT_TOKEN_CAP` bounds `AGENT_MODEL`/`SMALL_AGENT_MODEL` spend.
  The producer defers over-budget primary mail (it stays unread and is retried on a
  later poll) rather than dropping it, and notifies an eligible known sender at most
  once per day. All three hourly discovery scans call `process_email.defer` directly,
  bypassing the producer, so each checks the budget itself immediately before
  deferring - the event scan checks it before claiming any `event_recommendations`
  ledger row, since a committed pending row would otherwise suppress re-selection.
  `process_email` itself re-checks the budget for primary and proactive jobs as a
  race guard for work already queued when the cap trips. Proactive/synthetic
  rejections are silent (no sender to notify); every rejection audits
  `worker.message_rejected` with `reason="daily_token_budget_exhausted"`.
- **Introduction relay** (`email/relay.py`, `worker/tasks.py`): a recipient matching
  `hidden-<reply-token>@RELAY_DOMAIN` is handled before the agent path. Server code
  requires sender authentication, an `introduced` consent row, and exact membership in
  that pair, then resolves only the opposite participant. It sends through the existing
  SES/SMTP connection with `From: The Network <proxy>` and `Reply-To: proxy`, replaces
  every source routing header, and preserves the participant-authored MIME body, including
  plain/HTML alternatives and attachments, after the decoded body passes the intake size
  guard. It never renders, sanitizes, signs, or adds a footer to participant content. Invalid
  tokens, nonparticipants, non-introduced states, and revoked pairs fail closed. The existing
  sender rate limit still applies.
- **Outbound** (`email/outbound.py`): after the SMTP send succeeds, `send_reply` also
  appends the sent message to an IMAP folder (`imap_sent_folder`, default `Sent`),
  flagged `\Seen`, so the account looks like a normal end-to-end mailbox with both
  received and sent mail visible. This append is best-effort visibility, not part of the
  delivery guarantee - a failure there is caught and audit-logged
  (`email.imap_append.completed`, outcome `success`/`error`) but never fails the job or
  retries the send.
- **Agent** (`agent/core.py`): pydantic-ai ReAct agent. The untrusted email body is
  passed as **user-role** content, never concatenated into the system prompt. For a
  registered sender, a bounded newest-first projection of that person's sanitized
  memory gists is prepended inside an explicit untrusted-data delimiter; the query
  selects no raw memory text. Tools registered in `build_agent`; deps in
  `agent/deps.py` bind authenticated sender identity, inbound threading/quote/trace
  metadata, proactive person/event capabilities, per-run action counters, the session
  factory, and server-owned mutating-tool replay state.

## Data model

Application-owned tables live in `db/models.py`. Procrastinate maintains its own durable
queue tables in the same Postgres database.

**`people`** - pure identity / addressing / the security boundary:
- `id` (uuid str, pk) - opaque internal id, *the only person reference the LLM ever sees*
- `email` (unique, indexed) - resolved server-side by the mailer, never by the LLM
- `name`

**`memories`** - the agent's freeform substrate:
- `text` - freeform content, the source of truth
- `embedding` `Vector(1536)` - pgvector, HNSW cosine, for semantic recall
- `refs` `text[]` - the `people.id`s a memory concerns (0..N)
- `gist` - PII-stripped summary; the **only** thing cross-user search may return
- `created_at` - recency / perishability signal

Small server-owned operational tables enforce boundaries that cannot safely be left to
model reasoning:

- **`admin_nonces`** - replay protection for verified PGP/MIME admin requests
- **`rate_limits`** - durable counters for inbound and outbound quotas
- **`processed_messages`** - durable Message-ID idempotency beyond the IMAP `\Seen` flag
- **`banned_emails`** - normalized addresses blocked before agent execution
- **`primary_intake_state`** - durable primary-only pause reason and timestamp
- **`primary_intake_observations`** - keyed fingerprints and authentication/known-sender
  booleans; no raw sender, domain, subject, or body
- **`primary_intake_judge_state`** - idempotent observation cursor plus enum verdict/reason
- **`proactive_surfaces`** - recently surfaced opaque people pairs, used to rotate scan
  candidates through a configurable cooldown

**`introduction_consents`** - security state for anonymous relay introductions:
- one row per unordered person pair
- server-written consent flags and state (`proposed`, `one_consented`,
  `introduced`, `declined` (temporary cooldown), or `revoked` (permanent)
- opaque reply token used to associate authenticated inbound replies with a pair

The same token becomes the pair's stable hidden reply address after mutual consent; no
relay table or migration is needed. Mutual consent sends two separate introduction
messages, one per participant, rather than a group `To` header containing both real
addresses. Dovecot catch-all delivery, IMAP polling, and SES/SMTP sending remain the only
mail infrastructure; the application exposes no receiving endpoint.

This table is security/capability state, not a domain schema over memories. The model
cannot write consent or read identities from it.

Event recommendations add a similarly narrow operational exception to the freeform
substrate. Event meaning remains freeform; these tables hold only state that server code
must enforce:

- **`events`** - stable owner and series identity, a monotonic content version, raw
  owner-controlled text, sealed gist and embedding, freeform recurrence, expiry, and
  cancellation. A recurring series is one row and one stable id.
- **`event_recommendations`** - one consideration/delivery ledger row per stable event id
  and person, bound to the event version whose gist was evaluated. `notified_at` is written
  only after SMTP succeeds.
- **`event_suppressions`** - person-level event-FYI suppression. It is never consulted by
  the people-matching or introduction paths.

Raw event text, recurrence, and submitter identity do not enter cross-user search or agent
context. Event search projects only opaque event id, sealed gist, and necessary lifecycle
fields. The dedicated send capability resolves the recipient and composes fixed mail from
the stored gist server-side.

There is no `kind`/`direction`/`status`/`category`/`tags`. Those are distinctions the
agent draws from `text` at reasoning time, not columns the DB enforces. The cardinality
of `refs` is the only structure, and it is incidental:

- **0 refs** → general knowledge / agent notes ("a Rust meetup Thursday")
- **1 ref** → an attribute about a person ("just moved to Berlin")
- **2+ refs** → *also* an undirected edge between those people

## The graph is a projection, not a table

"Who knows whom" is derived at query time (`search/graph.py`): nodes are people, an edge
exists because some memory references both, edge weight comes from count/recency of shared
memories. NetworkX does multi-hop proximity math; the LLM does the language→reference
mapping at write time. Semantic match over memories lives in `search/match.py`.

## Agent surface - seventeen tools (`agent/tools.py`)

| tool | description |
|---|---|
| `remember(text, refs)` | write a chunk; a PII-stripped gist is auto-produced for any memory with refs |
| `forget(memory_id)` | delete a sender-owned, single-ref chunk (edit = forget + remember, so embeddings never go stale) |
| `search(query) -> [{person_id, evidence: [{gist}], similarity}]` | semantic candidate discovery grouped by opaque person id, with deterministic count/character bounds and **sealed gists only** for other people; sender-owned evidence items include their own `memory_id` for forget-plus-remember edits |
| `reply_to_sender(subject, body_text, sent_email_summary)` | reply only to the authenticated inbound sender; the model cannot select a recipient, and only this tool receives inbound threading and quoted-message context. An unfamiliar sender can be answered without registration; after SMTP succeeds, a known recipient gets the separate concise summary as a normal sealed memory |
| `send_first_contact_welcome()` | send fixed usage guidance to an authenticated unfamiliar sender; recipient, subject, body, threading, one-response-per-run enforcement, and daily quota are server-owned |
| `send_outreach(recipient_user_id, subject, body_text, sent_email_summary)` | send a new, unthreaded message to another user by opaque id; the address is resolved server-side, and the post-SMTP summary is remembered without storing the subject, body, address, or headers |
| `propose_introduction(other_person_id, sender_gist, other_gist)` | creates a pairwise proposal and sends fixed anonymous opt-in requests; authenticated replies are handled server-side before the model runs |
| `register_person(name)` | onboard an authenticated sender on first contact; self-registration only, with the address supplied from the verified inbound sender - the id it returns is what later `remember` calls key off |
| `escalate(reason)` | flag this email for human review and notify `admin_emails`; authenticated unknown senders also receive the fixed first-contact welcome when its server-side gates allow it. The fallback when no safe, useful action is clear |
| `no_action(reason)` | record that no reply, outreach, or memory is warranted (spam, automated mail, no genuine ask); a no-op that notifies no one - the explicit way to end a run without dispatching anything, so a deliberate no-op is distinguishable from a dropped response |
| `create_event(text, expires_at, recurrence)` | create one authenticated sender-owned event or recurring series; sanitize and embed its cross-user gist |
| `update_event(event_id, text, expires_at, recurrence)` | replace an authenticated owner's event content while preserving its stable id and refreshing its gist/embedding |
| `cancel_event(event_id)` | cancel an authenticated owner's event so it cannot be searched or recommended |
| `search_events(query)` | search active events through the gist-only projection; raw event text and owner identity are absent |
| `send_event_recommendation(event_id)` | send the scan-bound event to the current authenticated proactive recipient using fixed server-composed copy; event version, lifecycle, event suppression, and ledger deduplication are rechecked |
| `stop_event_recommendations()` | suppress event FYIs only for the authenticated sender |
| `resume_event_recommendations()` | remove only the authenticated sender's event-FYI suppression |

The event path is deliberately a one-way FYI service. It has no reminders, RSVP or
attendance tracking, follow-up, calendar integration, or people-recommendation opt-out.

## Stack

SQLModel over psycopg 3 · Alembic (the `CREATE EXTENSION vector` lives in a migration) ·
pgvector `Vector(1536)` HNSW cosine · pydantic-ai (multi-provider, chosen by config
string) · OpenAI `embed_text` wrapper (`embed/`, fixed at 1536 dimensions) · NetworkX ·
pydantic-settings · imap-tools · BeautifulSoup (HTML-to-visible-text fallback for inbound
bodies) · stdlib `EmailMessage`/`smtplib` · Procrastinate · `limits` · pytest +
pydantic-evals. Vendor-agnosticism comes from pydantic-ai, selected by `AGENT_MODEL` - no
LiteLLM, no proxy glue. `EMBED_MODEL` is OpenAI-only and is validated against the
`Vector(1536)` schema at startup.
