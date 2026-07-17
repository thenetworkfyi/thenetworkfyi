# Petra tier-2 expectation investigation

Date: 2026-07-17

## Decision

Gate Petra's provenance memory expectation on LLM-persona mode and report the
unavailable check as an explicit passing skip. Do not put the provenance detail
in Petra's scripted opening and do not add a Petra-specific deterministic reply
sequence.

This is a scorer applicability defect, not evidence that the product failed to
remember a disclosure. A non-LLM run cannot produce the disclosure that the
expectation requires.

## Evidence

`ScriptedTinyPerson` in [`thenetwork/sim/cli.py`](../thenetwork/sim/cli.py)
returns the configured `opening_body` for every stimulus. It does not inspect the
persona goal, a reply from The Network, or scheduled events.

Petra's authored opening in
[`thenetwork/sim/personas/population.py`](../thenetwork/sim/personas/population.py)
mentions only archival science and data management. Her goal deliberately says
to reveal provenance systems for museum archives only after a thoughtful
follow-up. The scripted responder therefore has no path that can emit the word
`provenance`, regardless of ticks or message budget.

The focused population test uses a conversational `QualifyingPetra` double to
prove the intended sequence: Petra first sends the vague opening, receives a
specific follow-up, then discloses provenance and satisfies the memory
expectation. That test does not use the CLI's scripted responder.

`run_sim` always registers all `DEFAULT_EXPECTATIONS`, including Petra's, while
`SimRunRecorder` scores every configured memory expectation without considering
`llm_personas`. `MemoryExpectation` has no mode-requirement fields or skip
semantics. In contrast, `OutcomeCheck` already has `requires_real_process` and
`requires_llm_personas`; unavailable outcome checks are emitted as passing
findings with `evidence.skipped: true` and an explanatory suffix.

The Taskwarrior acceptance criteria for the original population-scoring task
(`152db41a`) required all LLM-behavior-dependent default checks to declare
`requires_llm_personas` and required skipped findings to explain why they were
skipped. Petra's expectation was added by that task, but the requirement was
implemented only for `OutcomeCheck`. This history makes the unconditional Petra
finding an omission rather than an intentional scripted-mode stress failure.

The issue is not unique to Petra. Nadia's bakery update is supplied as a
scheduled stimulus, which the same scripted responder also ignores. Default
expectation applicability should therefore be represented by scorer metadata,
not by a persona-name conditional.

There is a second applicability edge: `--personas 10` excludes both Nadia and
Petra but still registers both default expectations. A follow-up implementation
should skip expectations whose bound `persona_email` is absent from the active
population, with a distinct reason.

## Recommended follow-up

Extend `MemoryExpectation` with capability requirements, following
`OutcomeCheck` rather than filtering expectations out in `run_sim`. At minimum,
add `requires_llm_personas`; default expectations driven by persisted product
memory should also declare their real-process requirement. Pass the run modes
and active persona emails into tier-2 scoring.

For each unavailable expectation, emit a normal `sim.score.tier2` finding with:

- `passed: true`;
- `evidence: {"skipped": true}`; and
- a message suffix naming every reason, such as `LLM-persona mode is disabled`
  or `persona is not in the active population`.

Keep the tier event and the expectation in `config.json`. Omitting either would
make an unexercised check look like it was never configured. `TierScore.passed`
can remain the conjunction of finding results, matching current outcome-score
semantics, as long as reviewers treat `evidence.skipped` as unavailable evidence
rather than a behavioral pass.

Mark Petra's default expectation as requiring LLM personas. Review and mark
Nadia's expectation in the same implementation because its scheduled disclosure
has the same dependency. Do not change Petra's opening: including provenance
there would destroy the qualification scenario. A stateful scripted reply would
also encode scenario control flow in the deterministic smoke responder, contrary
to the repository's prompt-emergent simulation design.

## Regression coverage

The follow-up can be verified without running a simulation:

1. A scoring unit test asserts that a Petra expectation requiring LLM personas
   becomes a passing skipped finding when `llm_personas=False`, including the
   exact reason and `evidence.skipped`.
2. The same expectation retains its existing pass and fail behavior when
   `llm_personas=True`.
3. A recorder unit test asserts that scripted mode still emits
   `sim.score.tier2`, with Petra skipped rather than failed.
4. A population-cap unit test asserts that an expectation bound to an excluded
   persona is skipped with the population reason.
5. Configuration serialization records the requirement fields so artifact
   review can reconstruct why a finding was eligible or skipped.

No simulation was run for this investigation.
