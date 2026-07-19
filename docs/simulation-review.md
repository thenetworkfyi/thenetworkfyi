# Reviewing simulation runs

This playbook is for reviewing artifacts produced by `sim run`. It is meant to make a
review reproducible: another reviewer should be able to reach the same conclusion from
the cited run artifacts and evidence.

Use the automated scores to find evidence, not as a substitute for reading the behavior under
review. LLM-persona runs are stochastic, so one run can demonstrate a concrete defect, but one
green run is not proof that a probabilistic behavior is reliable.

## Choose the review mode

Every review first evaluates one run on its own. A baseline is optional and adds a second,
comparative layer.

- **Isolated review:** evaluate a run against the configured scenario, security invariant,
  deterministic quality checks, outcome expectations, and the behavior visible in its
  transcript and audit trail. This mode can find product defects, scorer defects, and harness
  failures. It cannot establish that behavior improved or regressed relative to another code
  version.
- **Comparative review:** complete the isolated review, then compare it with a compatible run
  from before the change. This mode can support improvement and regression claims when the
  run provenance and configuration are known.

State the mode in the review. Do not invent a baseline for an isolated run or weaken an
isolated finding merely because there is no comparison.

## Set the run and optional baseline

```bash
RUN=runs/20260710T202053Z
# Comparative review only:
# BASELINE=runs/20260710T184522Z
```

For an isolated review, leave `BASELINE` unset and continue to the completion checks.

For a comparative review, choose a baseline that ran the code before the change and exercised
the same configuration. Do not choose a baseline merely because it is the newest directory.
Compare all configuration except the disposable database name:

```bash
diff -u \
  <(jq -S 'del(.database_name)' "$BASELINE/config.json") \
  <(jq -S 'del(.database_name)' "$RUN/config.json")
```

Any difference in scenario, modes, ticks, proactive cadence, persona definitions, message
budgets, or quality thresholds can explain a behavioral delta and must be called out. If a
configuration difference is relevant to the result, find a compatible baseline or switch to
an isolated review and report that no direct comparison is available.

`config.json` records the launching checkout's Git commit and whether the tree was dirty at
launch, under the `git` key:

```bash
jq '.git' "$RUN/config.json"
# {"commit": "<sha>", "dirty": <true|false>}
```

A `commit` of `null` means the run started outside a Git checkout or `git` was unavailable;
treat provenance as unknown in that case rather than guessing from directory or commit
timestamps. For runs recorded before this field existed, fall back to external run notes or
the launching terminal to establish provenance.

Current `config.json` files also include `runtime_provenance.version: 1`. That section
records public-safe model identifiers by role, whether each role was active,
behavior-affecting request and sanitizer settings, and SHA-256 fingerprints of the static
agent, persona template, and sanitizer prompts. The persona fingerprint is for the
unrendered template; persona identities, goals, messages, secrets, and credentials are
never included. Runs made before this section existed have unknown runtime and prompt
provenance. Do not infer it from the current checkout or retrofit it into an old run.

## Confirm the run completed

Do not score a run that is still active or was abandoned. A complete default-population run
has `sim.run_completed` as the final event and has a rendered transcript:

```bash
jq -r 'select(.event == "sim.run_completed")' "$RUN/events.jsonl"
tail -n 1 "$RUN/events.jsonl" | jq -r '.event'
test -f "$RUN/transcript.md"
```

The last command produces no output on success. The recorder renders `transcript.md` and
writes all score events before `sim.run_completed`. A directory containing only partial
mail, audit, or process events is useful for diagnosing a failed run, but it is not a
completed result and cannot receive a normal review verdict.

## Know which artifact answers which question

Review artifacts in this order:

1. `config.json` is authoritative for scenario inputs and run modes.
2. `events.jsonl` is authoritative for harness lifecycle, score findings, tick totals, and
   completion.
3. `audit.jsonl` is the privacy-safe structural trace of the production processing path. Use
   it for tool completions, consent transitions, errors, and trace correlation. It exists only
   for real-process runs. Simulation audit capture deliberately omits `agent.model_response`
   records because their freeform tool arguments can include raw owner-controlled event text;
   use the retained structural tool events rather than expecting model copy in this artifact.
4. `all-mail.mbox` and `transcript.md` are redacted, publishable views. They preserve
   message order and debugging structure, but they are not a source of raw identity or
   conversation content.
5. `private/all-mail.mbox` is the exact mail input used by deterministic scorers. It is
   owner-only, is not a normal review artifact, and must never be uploaded or supplied to an
   LLM. Access it only under an approved incident or reproducibility procedure, or the
   owner-only final-presentation procedure below, then delete it.
6. `transcript.md` is a derived, human-readable rendering of the redacted mbox. Its `Message N`
   headings use the same one-based indices cited by mail score findings.

Real-process databases are disposable unless the run used `--keep-db`. Tier 2 and outcome
findings capture the state assembled before database cleanup; do not expect to query that
database later.

## Read the scores

Start with a compact status list, then print only failed findings:

```bash
jq -r '
  select(.event | startswith("sim.score")) |
  [.event, (if .passed then "PASS" else "FAIL" end)] | @tsv
' "$RUN/events.jsonl"

jq -c '
  select(.event | startswith("sim.score")) |
  .event as $score |
  .findings[] |
  select(.passed == false) |
  {score: $score, message, evidence}
' "$RUN/events.jsonl"
```

Interpret the tiers as follows:

- `sim.score.tier1` is a security gate. It checks delivered mail for exact cross-persona PII
  strings, including fixed introduction mail after mutual consent. Consent authorizes only
  the anonymous relay handoff; it never exempts participant names or real addresses from
  server-authored mail. Preserved participant relay bodies are excluded because they are
  authenticated human correspondence and may voluntarily contain their author's identity;
  they bypass the agent and are not product-authored disclosure. Any failure is a stop-ship
  finding. A pass does not replace the full `tests/security/` suite or prove that every
  possible indirect disclosure is safe.
- `sim.score.presentation` checks captured automated and fixed-introduction MIME for
  plain-first alternatives, visible-text parity, required operational text, and unsafe or
  hidden HTML. Public findings identify only stable message indices and bounded violation
  codes; use the private owner-only procedure when the content itself must be examined.
- `sim.score.quality` checks deterministic mail-level failures: misrouted replies, noisy
  undispatched-response alerts, consent-request bursts, configured weak-match proposals,
  and malformed simulated consent tokens. Investigate every failure at its cited message
  indices.
- `sim.score.tier2` checks end-of-run memory expectations. A finding with
  `evidence.unexercised: true` means the expected fact was absent from that persona's private
  inbound mail; it is neither memory success nor a product defect. For exercised failures,
  confirm that the failure belongs to the expected persona; the evidence may identify a
  similar memory owned by someone else.
- `sim.score.outcome` checks scenario-specific observable behavior such as decline,
  clarification, dormancy, and consent state. A passing finding with
  `evidence.skipped: true` was not exercised and is not positive evidence.
- `sim.judge.*` is an optional LLM diagnostic, not a hard gate. It is normally absent from
  current real-process `sim run` artifacts.

Scores can also be wrong. If a finding conflicts with the transcript, audit chronology, or
documented predicate, classify it as a scorer defect and cite both sides of the contradiction.
Do not change the product to satisfy a faulty scorer.

## Inspect the cited behavior

Extract one message cited by a score finding:

```bash
MESSAGE=68
awk -v n="$MESSAGE" '
  $0 == "## Message " n { show = 1 }
  show && $0 ~ /^## Message / && $0 != "## Message " n { exit }
  show
' "$RUN/transcript.md"
```

When a non-redacted trace ID is available from the privacy-safe audit trail, use it to correlate
processing and tool activity. Do not try to reverse a redacted transcript or event value:

```bash
TRACE_ID=replace-with-trace-id
jq -c --arg trace "$TRACE_ID" 'select(.trace_id == $trace)' "$RUN/audit.jsonl"
```

Some agent-originated messages have a blank simulation trace header. For those, follow the
mail thread to the persona message that triggered the response and use that message's trace
ID; if no trace ID is available, correlate conservatively by thread, recipient, and audit
timestamp and state the uncertainty.

For consent behavior, inspect the chronology rather than only the final row status:

```bash
jq -c '
  select(
    .event == "introduction.consent_transition" or
    (.event == "agent.tool.completed" and .tool_name == "propose_introduction")
  ) |
  {timestamp, event, tool_name, action, consent_state, outcome, trace_id, sender_id_hash}
' "$RUN/audit.jsonl"
```

Then read the relevant transcript threads. Check that proposal messages are specific enough
to justify consent, replies stay bound to their inbound sender and thread, identity appears
only after mutual consent, declines and revocations are honored, and proactive activity does
not create repeated or irrelevant outreach.

Always inspect the behavior the scenario or code change is intended to exercise, even when
every score passes. Also inspect adjacent behavior that could be harmed. For example, a change
that reduces proposal volume should be checked both for fewer consent bursts and for
accidentally suppressing strong matches.

## Perform the owner-only Evolution presentation review

This is a narrow exception to the normal private-artifact policy. It exists so the owner can
approve the presentation of the exact delivered messages at final simulation sign-off. It is
not a way to make raw mail a shareable review artifact. Complete the automated and redacted
artifact review first, and skip this procedure unless the owner is conducting final sign-off.

Only the owner may perform this review, on an owner-controlled machine and Evolution profile
whose data is not synchronized or backed up to a third party. Do not expose
`private/all-mail.mbox`, an imported message, or a screenshot to an LLM, hosted analysis tool,
issue tracker, pull request, chat, shared drive, or other upload. This prohibition includes
asking an assistant to inspect the raw artifact. Keep `all-mail.mbox` and `transcript.md` as the
normal shareable mail artifacts.

### Prepare an owner-only copy

Copy the private mbox rather than importing the run artifact in place. The staging directory
and file must remain readable only by the owner:

```bash
RUN=runs/20260710T202053Z
umask 077
REVIEW_COPY_DIR="$(mktemp -d)"
cp -- "$RUN/private/all-mail.mbox" "$REVIEW_COPY_DIR/all-mail.mbox"
chmod 600 "$REVIEW_COPY_DIR/all-mail.mbox"
test -s "$REVIEW_COPY_DIR/all-mail.mbox"
```

Do not print, search, transform, or preview this copy in a terminal command whose output may be
captured. If the Evolution profile or its data directory is included in cloud sync, desktop
search, or machine backups, use a different owner-only profile that is excluded from those
services.

Before selecting or opening any imported message:

1. Start Evolution with `evolution --offline`, or choose **File > Work Offline**, and confirm
   that the connection icon is disconnected and **Send/Receive** is unavailable.
2. Under **Edit > Preferences > Mail Preferences > HTML Messages > Loading Remote Content**,
   select the option that never loads remote content. Disable any contact or sender exception.
   Do not use **View > Load Images** during the review.
3. Do not rely on Evolution's offline switch as a network sandbox: it applies to mail, not all
   Evolution components. Disconnect the machine from the network when practical. In every
   case, keep remote content disabled and do not open links or attachments.

### Import into a disposable local folder

Under **On This Computer**, create a local folder named for this review, such as
`sim-review-20260710T202053Z`. Do not choose a folder under an IMAP, Exchange, or other remote
account. Then choose **File > Import > Import a single file**, select
`$REVIEW_COPY_DIR/all-mail.mbox`, and choose the disposable local folder as the destination.
Evolution detects the mbox type from the file.

Review the message list, threads, plain-text and HTML alternatives, and the complete rendered
body. Do not reply, forward, print, save an attachment, open a link, or load remote content.
For each flow below, record `pass`, `fail`, or `N/A`; use `N/A` when `config.json`, the score
events, or the transcript shows that the run did not exercise the flow, and state that reason.
Never turn an unexercised flow into a pass.

| Flow | Presentation and chronology to inspect |
| --- | --- |
| Onboarding | Welcome and follow-up messages are readable, correctly threaded, and give usable next steps. |
| Clarification | Clarification requests and responses remain in the initiating thread and make the required action clear. |
| Consent | Proposals, decision instructions, and capability tokens are legible in both alternatives and are not hidden or clipped. |
| Decline | A decline is acknowledged without presenting it as consent or continuing the declined introduction. |
| Introduction | Each post-consent introduction is readable and anonymous, preserves plain-text and HTML alternatives, and uses the proxy reply path. |
| Relay and revoke | Relayed mail has coherent proxy headers and threading; revocation is clear, and no later relay is presented as delivered. |
| Event recommendation | The recommendation is readable, specific enough to act on, and does not expose submitter identity or unrelated member data. |

Across every exercised flow, also check alternative ordering and semantic parity, visible
subjects and sender labels, quoted-reply layout, long-line wrapping, capability-token
legibility, unexpected blank or hidden content, broken markup, and attempts to fetch remote
resources. The security and chronology requirements elsewhere in this playbook still apply;
attractive rendering cannot override a tier 1 or consent failure.

### Record evidence without copying private content

The shareable review report may contain the flow result, a short structural observation, and
citations to one-based message numbers in the redacted `transcript.md` or to privacy-safe
`events.jsonl` and `audit.jsonl` records. It must not contain raw names, addresses, subjects,
bodies, tokens, attachments, HTML, or screenshots from Evolution. When redaction removes the
detail needed to explain a presentation decision, record only a bounded owner attestation such
as `owner-only Evolution review: consent presentation passed`; do not reproduce the missing
content.

Screenshots are unnecessary for a normal sign-off. If one is required to diagnose a defect,
keep it owner-only outside the repository and all synchronized directories, give it owner-only
permissions, and delete it after the finding is resolved. Create a synthetic or redacted
reproduction for any issue, pull request, or shared report. Never use the private screenshot
itself as a public artifact.

Add this section to the report:

```markdown
## Owner-only presentation review

- Reviewer: `owner`
- Evolution offline and remote content blocked: `<yes or no>`
- Onboarding: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Clarification: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Consent: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Decline: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Introduction: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Relay and revoke: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Event recommendation: `<pass, fail, or N/A; redacted evidence or bounded owner attestation>`
- Imported copies removed: `<yes or no>`
```

### Remove every imported copy

Cleanup is part of sign-off, even when the verdict is reject or inconclusive:

1. Close all message windows. Delete the disposable folder under **On This Computer**, empty
   its local Trash, and use **Folder > Expunge** where available. Confirm that neither the
   folder nor its messages remain visible.
2. Quit Evolution so it releases the local store, then delete only the staged copy and its now
   empty directory:

   ```bash
   rm -- "$REVIEW_COPY_DIR/all-mail.mbox"
   rmdir -- "$REVIEW_COPY_DIR"
   ```

3. Delete any owner-only screenshots or temporary notes containing private content. Restore
   Evolution's normal online or remote-content settings only after the imported data is gone.
4. Apply the run retention policy to the original `private/` directory: delete it immediately
   after review unless a separately approved incident or reproducibility procedure requires
   temporary retention. Do not delete the redacted mbox or transcript used by the report.

If any imported copy cannot be accounted for and removed, record cleanup as failed and do not
approve final sign-off.

## Review an isolated run

An isolated verdict is based on the run's own evidence:

- Was the run complete, and did it exercise the required real-process and LLM-persona modes?
- Did tier 1 pass, and did every fixed introduction remain anonymous throughout the
  consent chronology?
- Did deterministic quality, memory, and scenario outcome checks pass without relevant skips?
- Does the transcript satisfy the scenario and persona expectations in `config.json`?
- Do the mail and audit artifacts reveal failures not covered by the current scorers?

An isolated run can be accepted when it completed, exercised the intended modes, passed its
required gates, and has no material behavioral defect in manual review. It can be rejected for
a concrete defect even without a baseline. Use language such as "the run exhibits" or "no
defect was observed"; do not say that behavior "improved," "regressed," or "stayed the same."

Because an LLM-persona run is one stochastic sample, distinguish "no defect observed in this
run" from a general guarantee. Request repeated runs when the decision depends on the frequency
or consistency of an emergent behavior.

## Compare with a baseline

This section applies only to a comparative review. First complete the isolated checks for both
the run and its baseline; a broken or incomplete baseline is not useful comparison evidence.

Compare like-for-like score findings and targeted transcript behavior. A useful comparison
answers:

- Did the intended failure disappear for the intended reason?
- Did any security, quality, memory, or outcome finding regress?
- Did message or proactive-job volume change enough to explain the result?
- Are differences caused by the code, configuration, or plausible persona variance?
- Does the transcript reveal a problem that no current scorer covers?

`sim compare` is supplemental only:

```bash
uv run sim compare "$BASELINE" "$RUN"
```

The current command reads `events.jsonl`. Real-process runs do not copy their audit tool calls
or model usage into that file, and they normally have no transcript-judge event. Consequently,
`sim compare` can report zero introductions, zero tokens, zero cost, and `n/a` judge scores for
a run that did real work. Never use those values as acceptance evidence without confirming
that the corresponding events exist.

## Classify the result

Use one of these classifications for each material observation:

- **Product defect:** an isolated run exhibits incorrect delivered behavior or stored state.
- **Product regression:** a comparative review shows that delivered behavior or stored state
  became worse, with evidence from compatible runs.
- **Expected behavior:** an isolated run satisfies the relevant invariant or scenario
  expectation.
- **Expected improvement:** a comparative review shows that the targeted behavior improved
  without a detected regression.
- **Persona variance:** an LLM persona took a different but valid path; explain why the result
  does not by itself establish a product defect or regression.
- **Scorer defect:** the automated interpretation conflicts with the underlying artifacts or
  intended invariant.
- **Harness/environment failure:** the run did not validly exercise the scenario.
- **Inconclusive:** provenance, required artifacts, evidence, or comparative baseline
  compatibility is insufficient.

When stochastic behavior is central to the verdict, request additional like-for-like runs
instead of averaging incompatible runs or declaring success from one sample.

## Review report template

```markdown
# Simulation review: <run directory>

## Verdict

<accept, reject, or inconclusive, with one-sentence reason>

## Provenance

- Review mode: `<isolated or comparative>`
- Run: `<path>`
- Baseline: `<path or not used>`
- Tested commit: `<config.json .git.commit, or unknown>`
- Working tree at launch: `<clean, dirty, or unknown, from config.json .git.dirty>`
- Configuration differences: `<none, list, or not applicable>`

## Automated findings

- Tier 1: `<pass/fail>`
- Quality: `<pass/fail and findings>`
- Tier 2: `<pass/fail and findings>`
- Outcomes: `<pass/fail/skipped findings>`

## Behavioral review

- `<scenario behavior or intended change>`: `<result with transcript and trace evidence>`
- `<adjacent invariant>`: `<result with evidence>`

## Baseline comparison

<comparative deltas with evidence, or "Not used for this isolated review.">

## Regressions and uncertainty

- `<product defect, regression, persona variance, scorer defect, harness issue, or none>`

## Follow-up

- `<code, scorer, harness, documentation, or additional-run action>`
```
