# THE SEAL - the security model

The critical concern: **prompt injection must not be able to exfiltrate user identities
or data, yet the agent must still email people on a user's behalf.** Leakage is made
*structurally impossible* rather than prompt-dependent. This is the single most important
invariant in the codebase - any change in `agent/`, `memory/`, `search/`, or `email/`
must preserve it and keep `tests/security/` green.

The problem: in a freeform store, a memory like *"Bob (bob@x.com) is a Rust dev looking
for a cofounder"* is raw PII inside `text`. Returning it to anyone but Bob would let a
prompt-injection exfiltrate it, so the privacy boundary cannot be "withhold a column."

## The layers

1. **Two-layer memory for person-referencing chunks.** Each carries a **raw form**
   (the durable substrate, read only by the sanitizer and the PGP-verified admin
   channel) and a **sanitized gist** (PII-stripped), which is the only form any
   search may return.
2. **Cross-user retrieval and the LLM only ever touch gists + opaque ids.** A hijacked
   model has no identifying text to leak. This applies to person memories and events;
   real addresses and event submitter identities never enter LLM context. The mailer
   resolves addresses server-side.
3. **Search projections are the chokepoints** (`search/match.py`, `search/events.py`):
   their SQL selects only sanitized gists, opaque ids, and the minimum lifecycle fields.
   Raw memory/event text, raw event recurrence, and event submitter identity never enter
   a cross-user result set, so there is no runtime branch a hijacked model can steer
   toward them.
4. **The sanitizer is a separate, narrowly-scoped step** (`memory/sanitize.py`): a
   mandatory Presidio pass redacts person names, email addresses, and phone numbers.
   Organizations and locations are deliberately kept in gists because those gists are what
   get embedded for company/place search recall. Quasi-identifying combinations are handled
   by the optional higher-fidelity LLM pass with a *fixed* prompt and no tools, run on
   `small_agent_model` (a cheaper/smaller model tier, separate from `AGENT_MODEL` which
   drives the main ReAct agent - this fixed-prompt subtask doesn't need the main agent's
   model). Missing Presidio is a deployment error, not a silent downgrade. The component
   that sees raw cross-user data stays small and auditable; the main agent never
   self-censors.
5. **Capability-style email tools (confused-deputy fix).** `reply_to_sender` has no
   recipient argument and derives its only recipient from the inbound sender.
   `send_outreach` takes an opaque `recipient_user_id`; the address is resolved
   server-side at send time. `send_event_recommendation` accepts only the opaque event
   id and content version bound into a server-authored trigger, derives the recipient
   from authenticated context, and composes fixed mail from the stored sanitized event
   gist only when that version is still current. The LLM never sees or supplies a raw
   address, submitter identity, subject, or event body.
6. **Double-opt-in anonymous handoff.** The model can propose an unordered pair but cannot
   record consent or compose the fixed introduction message. A random reply token associates an
   explicit `YES`, `NO`, or `REVOKE` reply with the pair; the worker accepts it only from
   an authenticated participant before model execution. After both participants consent,
   server code resolves both identities and sends two fixed, proxy-addressed messages,
   one to each participant. The body omits participant names and real addresses, prints
   the pair's relay address as an alternative to replying, and recaps the match from the
   proposal gists after server-side re-sanitization and snapshotting. Their real email
   addresses are never placed together in a message. `NO` records a temporary declined
   state (90-day configurable cooldown); `REVOKE` is permanent and revoked pairs remain
   structurally suppressed.
7. **Server-only address relay.** After introduction, both directions use the pair's
   `hidden-<reply-token>@RELAY_DOMAIN` address. The Dovecot catch-all feeds the ordinary
   IMAP worker; before any model, consent parser, memory write, or agent content scan,
   server code verifies the authenticated sender and current `introduced` state, resolves
   only the other participant, and sends through SES/SMTP. Relay mail always uses
   `From: The Network <proxy>` and the same `Reply-To`; it never copies the inbound display
   name or sender address. The original MIME body is preserved, including participant-authored
   plain/HTML alternatives and attachments, while every source routing header is discarded.
   This is address privacy only: participant names and message content are not masked,
   inspected by the agent or content scanner, sanitized, or re-rendered.
8. **Role separation.** Untrusted inbound body is user-role message content, never in the
   system prompt (`agent/core.py`).
9. **Mail-loop prevention (RFC 3834).** Inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*` is skipped; automated agent replies and fixed
   introductions set `Auto-Submitted: auto-replied`. Human-to-human relay messages omit it
   so normal mail clients treat them as correspondence. Simulation gates use this
   server-owned distinction rather than participant-authored subjects or bodies.
10. **Rate limiting / anti-DoS.** Disposable sender domains are rejected before job
   creation. Per-sender quota plus registration, outbound-recipient,
   outbound sender-reply, and first-contact welcome quotas use `limits` with Postgres-backed
   state so counters survive restarts. Keys are normalized and split by
   authentication state: authenticated senders use the normal bucket, while
   unauthenticated `From:` headers use a smaller unauthenticated bucket that cannot
   consume the matching real user's quota. A separate global emails-processed-per-hour
   bucket caps total LLM spend and fails closed if the rate-limit store is unavailable;
   bounded Procrastinate worker concurrency remains an additional ceiling. Outbound
   quota checks occur before SMTP and are consumed after a successful send, leaving a
   bounded cross-worker check-versus-consume race rather than charging failed sends.
   Optional primary-intake monitoring adds a server-side circuit breaker before enqueue:
   it stores only independently domain-separated keyed HMAC fingerprints, authentication
   and known-sender booleans, opaque trace/UID values, and timestamps. A rolling-hour burst
   pauses only primary intake before its batch is marked seen. The hourly abuse judge uses
   `small_agent_model` with a fixed prompt, strict enum output, no tools, and a bounded,
   sender-diverse 24-hour projection whose fingerprints are replaced with run-local opaque
   labels. It cannot read raw mail or identities, inspect relay mail, resume intake, or
   perform any action except returning a verdict. Only `coordinated_abuse` can atomically
   pause primary intake; model/provider failure and `suspicious` do not change intake and
   are audited.
   Ordinary primary mail remains unread while paused, while relay delivery and verified
   PGP administration bypass the pause.
11. **Audit correlation without PII.** Per-message `trace_id` values are minted as
   opaque UUIDv4-style tokens at IMAP intake and threaded through the Procrastinate
   job, worker, agent run, outbound SMTP send, and IMAP Sent append. Sender-level
   audit correlation must use `security/sender_identifier.py`, which derives
   `snd_v1_...` tokens by HMAC-SHA256 over a normalized sender address with the
   server-side `SENDER_IDENTIFIER_SECRET`. The digest is truncated before logging
   so audit entries can be correlated without storing raw email addresses or full
   keyed digests. If the secret is unset, no sender pseudonym is logged. Never log
   raw sender addresses, and never replace this with a bare `sha256(email)`:
   candidate-address dictionary lookup would make that reversible.
12. **Credentials.** Loaded from env / `.env` via pydantic-settings; never hardcoded.
13. **Optional content scanner.** LlamaFirewall's local Llama Prompt Guard 2 86M
    classifier is opt-in defense-in-depth, never the primary defense
    (`security/content_scan.py`). Agent-bound primary mail is scanned after its hard
    body cap and before consent parsing, person lookup, memory access, or the agent.
    Tokenizer-aware windows overlap and reserve special-token space so every window
    fits the model's 512-token context instead of relying on LlamaFirewall's silent
    truncation. A block or scanner error fails closed; only the fixed PII-safe reason
    category reaches audit, because LlamaFirewall's detailed block reason contains the
    raw email. Relay mail and verified PGP administration retain their earlier
    server-only bypasses.
14. **Server-owned mutating-tool replay boundary.** Within one agent run, validated
    mutating-tool names and arguments are canonically fingerprinted in memory and indexed
    by their occurrence before each Pydantic retry prompt. A later retry generation receives
    a structured replay of the completed result instead of repeating database, SMTP, quota,
    or sent-memory effects. The model supplies no idempotency key, raw arguments and their
    fingerprints are never audited, and distinct argument sets in the same trace still run.

## The admin channel

A separate concern from the SEAL above (which governs what the *agent* can leak about
users) - this is how a human operator proves they're the operator. `admin/auth.py`
requires all of: sender in `ADMIN_EMAILS`, subject starting with `ADMIN:` (a cheap
pre-filter only - RFC 3156 never signs headers, so the subject carries no authority),
the message a `multipart/signed` PGP/MIME (RFC 3156) envelope whose detached signature
verifies against `ADMIN_GPG_PUBLIC_KEY` using the byte-exact original signed part, and a
verified cleartext body containing a `COMMAND:` line. Freshness and replay protection
come from inside the verified signature itself, not from operator-typed tokens: the
OpenPGP signature packet carries its own creation timestamp (must be within
`ADMIN_REPLAY_WINDOW_SECONDS` of now) and the signature bytes are hashed and checked
against `admin_nonces` for reuse. Both values are cryptographically bound to a valid
signature, so neither can be forged or replayed independent of it - no hand-typed
`TS:`/`NONCE:` lines to author. Replay protection deliberately doesn't key off any
unsigned header (e.g. `Message-ID`): RFC 3156 never signs headers, so an attacker could
rewrite one on a captured signed email without invalidating the signature. The actual
command comes from the signed body's `COMMAND:` line, never the `Subject` header, so an
in-transit header rewrite can't swap which command runs without invalidating the
signature. No shared secret to generate or rotate by hand - any PGP/MIME-capable mail
client's "digitally sign" action produces a valid request.

## What the red-team enforces

`tests/security/` proves the property: adversarial emails must produce **zero** raw
other-person memory text - no names, emails, or bios - in the reply *or* in any tool
argument, even under a fully-hijacked model. If you change the seal/sanitize/search path,
the bar is not "the tests pass" but "no raw other-person text can reach LLM context or
egress." Treat a red-team failure as a structural break, not a flaky test.
