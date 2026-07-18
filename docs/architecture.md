# Architecture

The "big picture" that requires reading several files to reconstruct. See
@docs/design-decisions.md for the rationale behind each choice.

## Message flow

```
[Inbox] --IMAP--> Producer (imap-tools poll)
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
                    | existing Dovecot catch-all -> same IMAP inbox/job
                    v
          Worker pair resolver --SES/SMTP--> [Opposite participant]
```

There is **one** long-lived process (`thenetwork-worker` → `worker/tasks.py:main`). It
drains the Procrastinate queue *and*, via periodic tasks, polls IMAP every minute
(`producer.poll_inbox`) and runs three independent hourly discovery scans: graph and
semantic people matching plus semantic event recommendations. No separate producer daemon
is required; `thenetwork-producer` is just a manual one-shot poll for cron/debugging.

- **Producer** (`worker/producer.py`): polls IMAP for unseen mail, enqueues exactly one
  durable job per message, and marks the message seen *only after* enqueue. Durability
  lives in the Postgres job row, not the IMAP seen-flag - a mid-run crash means the job
  retries (`max_attempts=3`) and nothing is lost. The producer only ever flips `\Seen`;
  it never deletes or moves inbound mail, so everything the account has received stays
  in INBOX permanently, the same as a normal mailbox.
- **Inbound body extraction** (`email/inbound.py`): prefers imap-tools'
  `MailMessage.text`, falling back to `MailMessage.html` run through BeautifulSoup to
  recover visible text when a message has no plain-text part. No hand-rolled MIME
  walking or attachment traversal - imap-tools has already done that. The result is
  bounded by `MAX_BODY_CHARS`, which is the size guard for downstream scanners and model
  context; a message whose decoded body exceeds the hard reject limit is flagged rather
  than truncated silently.
- **Worker** (`worker/tasks.py`): Postgres-native Procrastinate (LISTEN/NOTIFY +
  `SKIP LOCKED`, no Redis/broker). Enforces per-sender rate limit and optional content
  scan, resolves whether the sender is a known `Person`, then calls
  `agent/core.py:run_agent_for_email`. Worker concurrency is the global LLM-spend ceiling.
- **Introduction relay** (`email/relay.py`, `worker/tasks.py`): a recipient matching
  `hidden-<reply-token>@RELAY_DOMAIN` is handled before the agent path. Server code
  requires sender authentication, an `introduced` consent row, and exact membership in
  that pair, then resolves only the opposite participant. It sends one plain message
  through the existing SES/SMTP connection with `From: The Network <proxy>` and
  `Reply-To: proxy`. Subject and bounded extracted body are copied without name/content
  rewriting. Invalid tokens, nonparticipants, non-introduced states, and revoked pairs
  fail closed. The existing sender rate limit still applies.
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
  `agent/deps.py` carry `sender_email` + `sender_user_id` (None on first contact).

## Data model

`db/models.py`. Everything durable is here.

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

**`introduction_consents`** - security state for identity-revealing introductions:
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

## Agent surface - sixteen tools (`agent/tools.py`)

| tool | description |
|---|---|
| `remember(text, refs)` | write a chunk; a PII-stripped gist is auto-produced for any memory with refs |
| `forget(memory_id)` | delete a sender-owned, single-ref chunk (edit = forget + remember, so embeddings never go stale) |
| `search(query) -> [{person_id, gist, similarity}]` | semantic recall returning **opaque ids + gist only** for other people |
| `reply_to_sender(subject, body_text, sent_email_summary)` | reply only to the registered inbound sender; the model cannot select a recipient, and only this tool receives inbound threading and quoted-message context. After SMTP succeeds, the separate concise summary becomes a normal sealed memory for that recipient |
| `send_outreach(recipient_user_id, subject, body_text, sent_email_summary)` | send a new, unthreaded message to another user by opaque id; the address is resolved server-side, and the post-SMTP summary is remembered without storing the subject, body, address, or headers |
| `propose_introduction(other_person_id, sender_gist, other_gist)` | creates a pairwise proposal and sends fixed anonymous opt-in requests; authenticated replies are handled server-side before the model runs |
| `register_person(name)` | onboard an authenticated sender on first contact; self-registration only, with the address supplied from the verified inbound sender - the id it returns is what later `remember` calls key off |
| `escalate(reason)` | flag this email for human review and notify `admin_emails`; no auto-reply is sent for true escalations. For authenticated unknown senders, it sends the fixed first-contact welcome instead of escalating. The fallback when no safe, useful action is clear (e.g. an unauthenticated first contact) |
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
