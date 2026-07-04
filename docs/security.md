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
   (retrievable only for that person's own requests) and a **sanitized gist** (PII-stripped),
   which is the only thing cross-user search may return.
2. **Cross-user retrieval and the LLM only ever touch gist + opaque ids.** A hijacked
   model has no identifying text to leak. Real addresses never enter LLM context - the
   mailer resolves them server-side.
3. **Self/other gate** (`memory/seal.py`): sole-ref-is-sender → raw text; otherwise →
   gist only.
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
5. **Capability-style email tool (confused-deputy fix).** `dispatch_email` takes an opaque
   `recipient_user_id`; the address is resolved server-side at send time. The LLM never
   sees or supplies a raw address.
6. **Role separation.** Untrusted inbound body is user-role message content, never in the
   system prompt (`agent/core.py`).
7. **Mail-loop prevention (RFC 3834).** Inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*` is skipped; all outbound sets
   `Auto-Submitted: auto-replied`.
8. **Rate limiting / anti-DoS.** Per-sender quota via `limits` with Postgres-backed
   state so counters survive restarts. Keys are normalized and split by
   authentication state: authenticated senders use the normal bucket, while
   unauthenticated `From:` headers use a smaller unauthenticated bucket that cannot
   consume the matching real user's quota. A separate global emails-processed-per-hour
   bucket caps total LLM spend and fails closed if the rate-limit store is unavailable;
   bounded Procrastinate worker concurrency remains an additional ceiling.
9. **Credentials.** Loaded from env / `.env` via pydantic-settings; never hardcoded.
10. **Optional content scanner.** Provider moderation / LLM Guard as opt-in
    defense-in-depth, never the primary defense (`security/content_scan.py`).

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
