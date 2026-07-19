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
   LLM. Access it only under an approved incident or reproducibility procedure, then delete it.
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
  this check. Any failure is a stop-ship finding. A pass does not replace the full
  `tests/security/` suite or prove that every possible indirect disclosure is safe.
- `sim.score.quality` checks deterministic mail-level failures: misrouted replies, noisy
  undispatched-response alerts, consent-request bursts, configured weak-match proposals,
  and malformed simulated consent tokens. Investigate every failure at its cited message
  indices.
- `sim.score.tier2` checks end-of-run memory expectations. Confirm that a failure belongs to
  the expected persona; the evidence may identify a similar memory owned by someone else.
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
