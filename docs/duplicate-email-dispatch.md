# Duplicate email tool calls

## Decision

Keep the existing server-owned exact replay boundary, and add a second,
capability-scoped dispatch gate if rephrased duplicate sends are observed in
production:

- `reply_to_sender` may deliver at most once in one agent run.
- `send_outreach` may deliver at most once per opaque `recipient_user_id` in one
  agent run.
- `send_first_contact_welcome`, event recommendations, and introduction consent
  requests retain their existing server-owned eligibility and ledger rules.

The gate should be based only on capability scope and opaque recipient identity.
It should not compare subjects or bodies, ask the model for an idempotency key,
or retain raw arguments. This preserves the SEAL and allows one run to contact
several distinct recipients while preventing a model from sending reworded
versions of the same email to one recipient.

This is a design conclusion, not a request to replace the current replay code.
The exact replay boundary already prevents the retry failure mode that motivated
the spike. The additional gate is a narrowly scoped hardening option for
semantically equivalent calls with different arguments.

## Root cause

Pydantic AI runs a multi-turn tool loop. A successful tool call is returned to
the model, which may make another call before producing its final output. The
framework also creates retry prompts for malformed tool calls or invalid final
output. A retry generation can repeat earlier calls from the transcript.

There are two distinct duplicate classes:

1. **Exact retry replay.** The model repeats the same validated tool name and
   arguments after a server-created retry prompt. Running the tool again would
   duplicate SMTP, quota, database, or memory effects.
2. **Rephrased duplicate intent.** The model calls the same email capability for
   the same recipient with a different subject, body, or summary. Exact argument
   hashing correctly treats this as a different call, even when a human would
   regard it as another version of the same reply.

The current implementation addresses the first class. `_idempotent_mutation`
canonically serializes validated arguments, hashes them with the tool name, and
indexes repeated occurrences within a retry generation. `AgentDeps` stores only
the fingerprints and completed structured results for the life of one run. On a
later retry generation, an exact occurrence returns `status=replayed` without
repeating the side effect. `tests/test_agent_tool_replay.py` demonstrates that a
repeated `reply_to_sender` sends only once while a genuinely different event
creation still executes.

The replay state deliberately expires with the run. If a model or provider
fails after SMTP succeeds, `run_agent_for_email` does not retry the whole job
with fresh replay state; it records `agent.failed_after_send` and treats the
successful send as authoritative.

The current mechanism does not collapse rephrased calls. A registered sender
can receive more than one `reply_to_sender` delivery in the same run until the
general dispatch cap is reached. Daily recipient limits bound abuse but do not
express the intended one-reply-per-inbound-message policy.

## Options considered

### Prompt-only constraints

The system prompt can ask the model to consolidate its response and call
`reply_to_sender` once. That guidance improves normal behavior but cannot be the
correctness boundary: prompt injection or a retry can still produce another
call. Keep such wording only as model guidance.

### Tool argument validation

Pydantic validation can reject malformed types and missing fields. It cannot
reliably decide whether two well-formed bodies express the same intent. Adding
semantic comparison to validation would require content retention or another
model call and would still be probabilistic.

### Exact server-owned replay

The implemented fingerprint-and-occurrence cache is the correct policy for
framework retries. It is deterministic, retains no raw arguments, permits
distinct calls, and returns the original structured result. It should remain
the first layer.

### Capability-scoped per-run dispatch gate

This is the recommended hardening for rephrased duplicates. Record successful
logical delivery channels in `AgentDeps`:

```text
sender_reply_sent: bool
outreach_recipients_sent: set[opaque person id]
```

After exact replay handling, `_send_email` checks the appropriate channel. A
second sender reply returns a structured result such as:

```json
{
  "status": "suppressed",
  "reason": "recipient_already_contacted_this_run"
}
```

For outreach, the key is the model-visible opaque person id, never an address.
The channel is recorded only after SMTP succeeds. A failed delivery therefore
does not prevent a legitimate retry. The existing exact replay cache then stores
the sent or suppressed result normally.

This policy preserves useful multi-recipient behavior:

- One run may call `send_outreach` once for each of several distinct opaque
  recipients.
- `propose_introduction` remains one server-owned pair operation that sends its
  two fixed consent requests. It does not use `send_outreach` and is governed by
  the unordered-pair ledger and proposal caps.
- A human can continue a conversation in a later inbound email, which creates a
  new run and a new sender-reply channel.

The tradeoff is deliberate: one inbound message cannot produce two separate
direct replies to its sender. The model must consolidate them into one email.
That matches the product interaction and avoids making semantic similarity part
of a security boundary.

### Deferred transactional email dispatch

Another design is to have email tools stage messages in an in-memory outbox,
coalesce or reject conflicts after model completion, and dispatch only the final
set. This gives the server a complete view before SMTP, but it changes tool
semantics from "sent" to "staged", delays delivery errors until after reasoning,
and requires a durable idempotent outbox to handle process failure safely. It is
appropriate only if the product later needs atomic multi-message plans. It is
more machinery than the current duplicate failure warrants.

## Proposed implementation boundary

If production evidence shows rephrased duplicates, implement the
capability-scoped gate in the existing agent dependency and email capability
layer:

1. Add the two per-run channel fields to `AgentDeps` with empty defaults.
2. Check them inside `_send_email` after authentication, recipient resolution,
   and policy validation but before quota consumption or SMTP.
3. Mark a channel immediately after successful SMTP, before best-effort sent
   memory recording.
4. Return a structured `suppressed` result for a repeated channel and emit only
   bounded audit fields (`tool_name`, `outcome`, and fixed `reason`).
5. Leave `_idempotent_mutation`, daily limits, introduction ledgers, and
   server-bound proactive capabilities unchanged.

Required regression cases are:

- an exact retry of `reply_to_sender` returns `replayed` and sends once;
- a rephrased second `reply_to_sender` returns `suppressed` and sends once;
- two differently worded `send_outreach` calls to the same opaque recipient
  send once;
- outreach to two different opaque recipients sends twice;
- an SMTP failure does not close the channel;
- a two-person introduction still sends both fixed consent requests; and
- replay and suppression audit events contain no arguments, addresses, or
  message content.

## Recommendation

No transactional outbox or semantic deduplication service is justified for this
case. Retain exact replay for framework retries. If rephrased duplicates recur,
add the deterministic per-run channel gate described above. It is the smallest
server-owned rule that prevents duplicate or reworded email delivery without
reducing legitimate multi-recipient introduction and outreach flows.
