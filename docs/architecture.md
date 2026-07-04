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
                    | tools: remember / forget / search / dispatch_email / escalate / register_person
                    v
                Reply --SMTP--> [Sender]
                    | append to IMAP Sent folder (best-effort, post-send)
                    v
              [Sent folder]
```

There is **one** long-lived process (`thenetwork-worker` → `worker/tasks.py:main`). It
drains the Procrastinate queue *and*, via periodic tasks, polls IMAP every minute
(`producer.poll_inbox`) and runs the hourly proactive scan. No separate producer daemon
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
- **Outbound** (`email/outbound.py`): after the SMTP send succeeds, `send_reply` also
  appends the sent message to an IMAP folder (`imap_sent_folder`, default `Sent`),
  flagged `\Seen`, so the account looks like a normal end-to-end mailbox with both
  received and sent mail visible. This append is best-effort visibility, not part of the
  delivery guarantee - a failure there is caught and audit-logged
  (`email.imap_append.completed`, outcome `success`/`error`) but never fails the job or
  retries the send.
- **Agent** (`agent/core.py`): pydantic-ai ReAct agent. The untrusted email body is
  passed as **user-role** content (`f"Subject: {subject}\n\n{body}"`), never concatenated
  into the system prompt. Tools registered in `build_agent`; deps in `agent/deps.py`
  carry `sender_email` + `sender_user_id` (None on first contact).

## Data model - two tables, that is all

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

## Agent surface - six tools (`agent/tools.py`)

| tool | description |
|---|---|
| `remember(text, refs)` | write a chunk; a PII-stripped gist is auto-produced for any memory with refs |
| `forget(memory_id)` | delete a chunk (edit = forget + remember, so embeddings never go stale) |
| `search(query) -> [{person_id, gist, similarity}]` | semantic recall returning **opaque ids + gist only** for other people |
| `dispatch_email(recipient_user_id, …)` | opaque id in; the real address is resolved server-side at send time |
| `register_person(email, name)` | onboard the sender on first contact; self-registration only (must match the authenticated From, must not already exist) - the id it returns is what later `remember`/`dispatch_email` calls key off |
| `escalate(reason)` | flag this email for human review and notify `admin_emails`; no auto-reply is sent for true escalations. For authenticated unknown senders, it sends the fixed first-contact welcome instead of escalating. The fallback when no safe, useful action is clear (e.g. an unauthenticated first contact) |

## Stack

SQLModel over psycopg 3 · Alembic (the `CREATE EXTENSION vector` lives in a migration) ·
pgvector `Vector(1536)` HNSW cosine · pydantic-ai (multi-provider, chosen by config
string) · provider-agnostic `embed_text` wrapper (`embed/`) · NetworkX · pydantic-settings ·
imap-tools · BeautifulSoup (HTML-to-visible-text fallback for inbound bodies) · stdlib
`EmailMessage`/`smtplib` · Procrastinate · `limits` · pytest +
pydantic-evals. Vendor-agnosticism comes from pydantic-ai and the embedding wrapper being
multi-provider, selected by `AGENT_MODEL` / `EMBED_MODEL` - no LiteLLM, no proxy glue.
