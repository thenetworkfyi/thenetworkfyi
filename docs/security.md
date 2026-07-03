# THE SEAL — the security model

The critical concern: **prompt injection must not be able to exfiltrate user identities
or data, yet the agent must still email people on a user's behalf.** Leakage is made
*structurally impossible* rather than prompt-dependent. This is the single most important
invariant in the codebase — any change in `agent/`, `memory/`, `search/`, or `email/`
must preserve it and keep `tests/security/` green.

The problem: in a freeform store, a memory like *"Bob (bob@x.com) is a Rust dev looking
for a cofounder"* is raw PII inside `text`. Returning it to anyone but Bob would let a
prompt-injection exfiltrate it, so the privacy boundary cannot be "withhold a column."

## The layers

1. **Two-layer memory for person-referencing chunks.** Each carries a **raw form**
   (retrievable only for that person's own requests) and a **sanitized gist** (PII-stripped),
   which is the only thing cross-user search may return.
2. **Cross-user retrieval and the LLM only ever touch gist + opaque ids.** A hijacked
   model has no identifying text to leak. Real addresses never enter LLM context — the
   mailer resolves them server-side.
3. **Self/other gate** (`memory/seal.py`): sole-ref-is-sender → raw text; otherwise →
   gist only.
4. **The sanitizer is a separate, narrowly-scoped step** (`memory/sanitize.py`): a
   deterministic PII strip (emails, phones), strengthened with Presidio NER redaction of
   person names, organizations, and locations when the optional `pii-ner` extra is
   installed (falls back to the regex-only strip if it isn't), plus an optional
   higher-fidelity LLM pass with a *fixed* prompt and no tools. The component that sees
   raw cross-user data stays small and auditable; the main agent never self-censors.
5. **Capability-style email tool (confused-deputy fix).** `dispatch_email` takes an opaque
   `recipient_user_id`; the address is resolved server-side at send time. The LLM never
   sees or supplies a raw address.
6. **Role separation.** Untrusted inbound body is user-role message content, never in the
   system prompt (`agent/core.py`).
7. **Mail-loop prevention (RFC 3834).** Inbound carrying `Auto-Submitted` /
   `Precedence: bulk|list` / `List-*` is skipped; all outbound sets
   `Auto-Submitted: auto-replied`.
8. **Rate limiting / anti-DoS.** Per-sender quota via `limits` (Postgres-backed), plus
   bounded Procrastinate worker concurrency as the global LLM-spend ceiling.
9. **Credentials.** Loaded from env / `.env` via pydantic-settings; never hardcoded.
10. **Optional content scanner.** Provider moderation / LLM Guard as opt-in
    defense-in-depth, never the primary defense (`security/content_scan.py`).

## What the red-team enforces

`tests/security/` proves the property: adversarial emails must produce **zero** raw
other-person memory text — no names, emails, or bios — in the reply *or* in any tool
argument, even under a fully-hijacked model. If you change the seal/sanitize/search path,
the bar is not "the tests pass" but "no raw other-person text can reach LLM context or
egress." Treat a red-team failure as a structural break, not a flaky test.
