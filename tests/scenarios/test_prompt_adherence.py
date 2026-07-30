"""Per-commitment adherence harness for the judgment-notes bullets.

`tests/test_prompts.py` proves each judgment-notes commitment is PRESENT in the
mode prompt that should carry it; it says nothing about whether the model
actually FOLLOWS one under realistic pressure. This module is that instrument:
one `pydantic_evals.Case` per commitment (reusing
`tests/scenarios/test_live_archetypes.py`'s dataset where a case already
exercises a commitment, adding a case here where none does), each scoring an
observable action - a tool call, a reply's shape, a memory's contents - never
prompt-text presence.

There is no flat prompt to measure any more. `prompts.SYSTEM_PROMPTS` holds one
assembled prompt per run mode, and each case reaches its mode the same way
production does: `run_scenario` selects the mode from the scenario's
authenticated/known/proactive shape, so a proactive case is scored against the
trigger prompt and its narrower tool set, not against a superset.

Three offline, fast tests guard the harness's own shape and always run under
`pytest -m "not integration"`:
  - every bullet in `prompts.JUDGMENT_BULLETS` maps to exactly one
    `Commitment` here, matched by the bullet's stable slug, so neither
    reordering nor rewording a bullet breaks the mapping - only adding,
    splitting, or deleting one does, which is exactly the drift this guards
    against;
  - every `Commitment` names at least one real, collected `Case`;
  - the scoring and comparison helpers behave against fake report/record
    shapes.

The actual measurement - the 32 parametrizations of
`test_prompt_adherence_case` - calls the real configured
`AGENT_MODEL`/`TEST_LLM_JUDGE_MODEL` the same way
`test_live_archetypes.py` does: marked `integration` + `live_model`, requiring
credentials only while recording, with real isolated pgvector schemas and
deterministic outbound mail. Each case has its own cassette, so a failed
recording can be retried by node id without paying for cases that already
succeeded. Run it deliberately:

    uv run pytest -m live_model --record-mode=once \
        tests/scenarios/test_prompt_adherence.py::test_prompt_adherence_case

`BASELINE_PATH` (`docs/notes/prompt-adherence-baseline.json`) is the immutable
flat-prompt reference measured before this chain started - a run never
overwrites it, because a comparison needs a fixed reference point. Each run
appends a record to `MEASUREMENTS_PATH`
(`docs/notes/prompt-adherence-measurements.json`) carrying the per-commitment rate,
the model, the commit, and the per-commitment comparison against the baseline.
Commitments that did not exist at the baseline - the bullets this chain split
into interactive and proactive variants - are recorded as
`new_since_baseline` rather than being scored as a regression from zero.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.retries import RetryConfig
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge
from tenacity import retry_if_exception, stop_after_attempt, wait_exponential_jitter

from thenetwork.agent.prompts import JUDGMENT_BULLETS
from thenetwork.model_config import model_with_api_key
from thenetwork.settings import get_settings
from tests.scenarios.test_live_archetypes import (
    DidNotDispatchEmail,
    EmailScenario,
    NoPersonalSignoff,
    RunOutcome,
    ToolCalledAtMostOnce,
    ToolWasCalled,
    ToolWasNotCalled,
    _judge_model,
    _provider_api_key,
    _snapshot_record,
    _skip_without_credentials,
    _strong_event,
    archetype_dataset,
    run_scenario,
)

_NOTES = Path(__file__).resolve().parents[2] / "docs" / "notes"

#: The flat-prompt reference. Read, never written - a comparison needs a fixed
#: point, and re-measuring the flat prompt is impossible now that it is gone.
BASELINE_PATH = _NOTES / "prompt-adherence-baseline.json"

#: Append-only log of later measurements, newest last.
MEASUREMENTS_PATH = _NOTES / "prompt-adherence-measurements.json"

#: Recorded on every measurement so a reader cannot mistake the delta for a
#: pure prompt-shape effect: the scenario harness itself changed after the
#: baseline was recorded (a MagicMock database session replaced by a real
#: migrated pgvector schema, and `forget_attempts` now collected from `forget`
#: tool call arguments rather than intercepted session lookups).
HARNESS_CHANGE_NOTE = (
    "the scenario harness changed between the baseline and this run: the "
    "MagicMock database session was replaced by a real migrated pgvector "
    "schema, and forget_attempts is now collected from forget tool call "
    "arguments rather than from intercepted session lookups - do not "
    "attribute the delta to the prompt shape alone"
)

#: The two commitments that moved off a 0.0 baseline, and are therefore the
#: ones most likely confounded by the harness change above rather than by any
#: prompt-shape difference.
CONFOUNDED_BY_HARNESS_CHANGE = frozenset(
    {"progressive_qualification_memory", "sender_owned_evidence_memory_ids"}
)


def _retry_on_rate_limit() -> RetryConfig:
    """Retry a whole case when the provider throttles it.

    Concurrency alone does not solve this. A measured run at concurrency 2
    still drew 89 HTTP 429s, because the limit is on the key, not on how
    politely this process paces itself - so the answer is to wait out the
    throttle rather than to keep shrinking the batch and paying for it in wall
    time. Only 429 is retried: any other model error is a real failure that
    should surface, not be papered over by five more attempts.
    """
    return RetryConfig(
        retry=retry_if_exception(
            lambda exc: isinstance(exc, ModelHTTPError) and exc.status_code == 429
        ),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# New evaluators for commitments the existing archetype dataset doesn't cover
# ---------------------------------------------------------------------------


@dataclass(repr=False)
class MentionsUnreadAttachment(Evaluator[EmailScenario, RunOutcome, object]):
    """The attachment bullet requires an accurate, non-inventive notice."""

    def evaluate(
        self, ctx: EvaluatorContext[EmailScenario, RunOutcome, object]
    ) -> bool:
        blob = "\n".join(d["body"] for d in ctx.output.dispatched).lower()
        return "attach" in blob and any(
            word in blob for word in ("paste", "include", "share", "send over")
        )


_LINK_FETCH_CLAIM_MARKERS = (
    "i opened",
    "i visited",
    "i read the page",
    "i checked the link",
    "the page says",
    "according to the link",
    "based on the page",
    "the site says",
)


@dataclass(repr=False)
class DoesNotClaimToHaveFetchedLink(Evaluator[EmailScenario, RunOutcome, object]):
    """The links bullet: a URL is a reference, never fetched page content."""

    def evaluate(
        self, ctx: EvaluatorContext[EmailScenario, RunOutcome, object]
    ) -> bool:
        bodies = [ctx.output.reply] + [d["body"] for d in ctx.output.dispatched]
        blob = "\n".join(b for b in bodies if b).lower()
        return not any(marker in blob for marker in _LINK_FETCH_CLAIM_MARKERS)


# ---------------------------------------------------------------------------
# New cases for commitments the existing archetype dataset doesn't cover
# ---------------------------------------------------------------------------

attachment_awareness_case = Case(
    name="attachment_awareness",
    inputs=EmailScenario(
        subject="Portfolio attached",
        body=(
            "Hi, I've attached my resume and a portfolio PDF - take a look and "
            "let me know who I should meet."
        ),
        sender_email="jordan@example.com",
        sender_user_id="user-jordan",
        sender_authenticated=True,
        attachment_count=2,
    ),
    evaluators=(
        ToolWasCalled("reply_to_sender"),
        MentionsUnreadAttachment(),
        NoPersonalSignoff(),
        LLMJudge(
            rubric=(
                "The server tells the agent two attachments are present but not "
                "read. A reasonable response tells the sender the attachment "
                "content was not read and asks them to paste the relevant "
                "details into the email body, without inventing or guessing "
                "what the resume or portfolio contain."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

link_reference_only_case = Case(
    name="link_reference_only",
    inputs=EmailScenario(
        subject="Check this out",
        body=(
            "I think this project is relevant to what I'm building: "
            "https://example.com/projects/some-relevant-open-source-tool-repo. "
            "Let me know if it's useful context."
        ),
        sender_email="devon@example.com",
        sender_user_id="user-devon",
        sender_authenticated=True,
    ),
    evaluators=(
        ToolWasCalled("reply_to_sender"),
        DoesNotClaimToHaveFetchedLink(),
        NoPersonalSignoff(),
        LLMJudge(
            rubric=(
                "The sender shared a URL as a reference. The agent cannot open "
                "links or read what's behind them - a reasonable response "
                "treats the URL as a visible label, not as page content, never "
                "claims to know what the page says, and if the destination's "
                "content actually matters, asks the sender what is behind the "
                "link rather than guessing."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

breadth_is_one_fact_case = Case(
    name="breadth_is_one_fact",
    inputs=EmailScenario(
        subject="Not sure exactly what I want",
        body=(
            "Honestly I'd be interested in machine learning, or maybe "
            "blockchain, or robotics, or biotech - could be any of those, not "
            "sure yet."
        ),
        sender_email="vic@example.com",
        sender_user_id="user-vic",
        sender_authenticated=True,
    ),
    evaluators=(
        ToolWasCalled("remember"),
        ToolCalledAtMostOnce("remember"),
        NoPersonalSignoff(),
        LLMJudge(
            rubric=(
                "The sender names four unrelated fields at once with no "
                "commitment to any of them - this is evidence that the ask "
                "itself is broad, not four separate durable facts. A "
                "reasonable response stores this breadth as one standing note "
                "in the sender's own words rather than one durable fact per "
                "named field, and does not treat any single field as a "
                "settled interest."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

unfamiliar_sender_declines_case = Case(
    name="unfamiliar_sender_declines",
    inputs=EmailScenario(
        subject="No thanks",
        body=(
            "I got this address from a friend but I'd rather not have "
            "anything about me stored or shared. Please don't keep any of my "
            "information."
        ),
        sender_email="private@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    ),
    evaluators=(
        ToolWasNotCalled("register_person"),
        ToolWasNotCalled("remember"),
        ToolWasNotCalled("escalate"),
        ToolWasCalled("reply_to_sender"),
        NoPersonalSignoff(),
        LLMJudge(
            rubric=(
                "The unfamiliar sender explicitly declines participation and "
                "data retention. A reasonable response acknowledges this "
                "briefly without registering them or saving any memory about "
                "them, and without escalating - this is not the kind of "
                "request that needs a human operator."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

first_contact_explains_service_case = Case(
    name="first_contact_explains_service",
    inputs=EmailScenario(
        subject="What is this?",
        body=(
            "I got this email address from a coworker. What exactly is this "
            "and how does it work?"
        ),
        sender_email="curious@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    ),
    evaluators=(
        ToolWasNotCalled("register_person"),
        ToolWasCalled("reply_to_sender"),
        NoPersonalSignoff(),
        LLMJudge(
            rubric=(
                "An unfamiliar sender is only asking what the service is and "
                "how it works, not sharing anything to register. A reasonable "
                "response answers the actual question in plain, user-facing "
                "language - an email address people can tell about what "
                "they're working on or who they'd like to meet, which asks "
                "both people before making an introduction - without "
                "registering them and without using internal design jargon "
                "such as 'autonomous connector', 'profile database', "
                "'substrate', or 'two-sided match thesis'."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

empty_body_first_contact_case = Case(
    name="empty_body_first_contact",
    inputs=EmailScenario(
        subject="Hi",
        body="",
        sender_email="blank@example.com",
        sender_user_id=None,
        sender_authenticated=True,
    ),
    evaluators=(
        ToolWasCalled("send_first_contact_welcome"),
        ToolWasNotCalled("register_person"),
        ToolWasNotCalled("reply_to_sender"),
        LLMJudge(
            rubric=(
                "The authenticated sender's message is just a greeting with an "
                "empty body - too little context to answer or register. A "
                "reasonable response sends the fixed first-contact welcome "
                "rather than guessing at intent, registering the sender, or "
                "escalating."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

proactive_preference_mismatch_case = Case(
    name="proactive_preference_mismatch",
    inputs=EmailScenario(
        subject="[Proactive] Possible connection",
        body=(
            "[System match] A standing signal about one person closely matches a "
            "standing signal about another (similarity=0.88).\n\n"
            "Person user-noor: works on ML infrastructure and has said they want "
            "to meet experienced peers, not beginners.\n"
            "Person person-hobbyist-2: self-taught hobbyist experimenting with ML "
            "tooling on weekends, new to the field and eager to learn from "
            "anyone.\n\n"
            "You are acting for person user-noor. If the pair is genuinely "
            "supported, the bound counterpart id is person-hobbyist-2."
        ),
        sender_email="noor@example.com",
        sender_user_id="user-noor",
        sender_authenticated=True,
        is_proactive=True,
        proactive_candidate_id="person-hobbyist-2",
    ),
    evaluators=(
        ToolWasCalled("no_action"),
        ToolWasNotCalled("propose_introduction"),
        DidNotDispatchEmail(),
        LLMJudge(
            rubric=(
                "The person being acted for stated a preference for experienced "
                "peers, and the counterpart's gist directly contradicts it - a "
                "self-taught hobbyist new to the field. A stated preference is a "
                "constraint on the match, not decoration, so high topic "
                "similarity does not rescue it. The only reasonable action is "
                "no_action; it must not propose the introduction."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

proactive_capped_capability_case = Case(
    name="proactive_capped_capability",
    inputs=EmailScenario(
        subject="[Proactive] Possible connection",
        body=(
            "[System match] A standing signal about one person closely matches a "
            "standing signal about another (similarity=0.87).\n\n"
            "Person user-rhea: runs a bakery-supply co-op and has been asking for "
            "someone with food-logistics distribution experience to help design "
            "their delivery routing.\n"
            "Person person-logistics-lead: ten years routing perishable-goods "
            "deliveries for regional food distributors, has said they want to "
            "advise small co-ops.\n\n"
            "You are acting for person user-rhea. If the pair is genuinely "
            "supported, the bound counterpart id is person-logistics-lead."
        ),
        sender_email="rhea@example.com",
        sender_user_id="user-rhea",
        sender_authenticated=True,
        is_proactive=True,
        proactive_candidate_id="person-logistics-lead",
        # The pair IS well supported, so the model should try to propose it -
        # and hit the already-exhausted per-run cap, which returns
        # `status=deferred`. This is the only status-vocabulary pressure that
        # can exist on a trigger: no reply and no escalation are registered.
        introduction_proposal_count=get_settings().introduction_max_proposals_per_run,
    ),
    evaluators=(
        ToolCalledAtMostOnce("propose_introduction"),
        ToolWasCalled("no_action"),
        ToolWasNotCalled("send_outreach"),
        ToolWasNotCalled("reply_to_sender"),
        DidNotDispatchEmail(),
        LLMJudge(
            rubric=(
                "This proactive trigger describes a genuinely well-supported "
                "two-sided match, so attempting propose_introduction once is "
                "correct. That call returns status=deferred because a "
                "server-side per-run cap is already exhausted. A deferred status "
                "is not an error to work around: a reasonable response does not "
                "retry the same call, does not try to reach either person some "
                "other way, and ends the run by recording no_action. There is no "
                "reply or escalation path on a trigger run."
            ),
            model=_judge_model,
            include_input=True,
        ),
    ),
)

_NEW_CASES = (
    attachment_awareness_case,
    link_reference_only_case,
    breadth_is_one_fact_case,
    unfamiliar_sender_declines_case,
    first_contact_explains_service_case,
    empty_body_first_contact_case,
    proactive_preference_mismatch_case,
    proactive_capped_capability_case,
)


# ---------------------------------------------------------------------------
# The commitment -> case(s) mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Commitment:
    """One judgment-notes bullet, identified by that bullet's stable slug.

    `slug` must name exactly one entry in `prompts.JUDGMENT_BULLETS` - see
    `test_every_judgment_bullet_has_exactly_one_mapped_commitment`. Keying on
    the slug rather than on position or text keeps the mapping valid across a
    bullet *reorder* or *reword*; adding, splitting, or deleting a bullet is
    what invalidates it, which is exactly the drift this guards against.

    `prefix` is a human-readable label, recorded in the committed baseline so
    a stored measurement stays legible without loading `prompts.py`. It is
    asserted to still lead its bullet, but it is deliberately no longer the
    identity: when a commitment applies in every mode yet names a tool only
    some modes register, the bullet is split into an interactive bullet and a
    proactive variant that open with the same words
    (`tool_status_vocabulary` / `tool_status_vocabulary_proactive`).
    """

    slug: str
    prefix: str
    case_names: tuple[str, ...]


COMMITMENTS: tuple[Commitment, ...] = (
    Commitment("attachments", "- Attachments:", ("attachment_awareness",)),
    Commitment("links", "- Links:", ("link_reference_only",)),
    Commitment(
        "search_similarity_discovery_only",
        "- `search` similarity is",
        ("job_keyword_qualification", "strong_match"),
    ),
    Commitment(
        "sender_owned_evidence_memory_ids",
        "- A `search` candidate marked",
        ("specific_fact_removal",),
    ),
    Commitment(
        "tool_status_vocabulary",
        "- Tool status vocabulary:",
        ("exhausted_reply_cap",),
    ),
    Commitment(
        "tool_status_vocabulary_proactive",
        "- Tool status vocabulary:",
        ("proactive_capped_capability",),
    ),
    Commitment(
        "forget_ownership",
        "- `forget` deletion is only appropriate",
        ("specific_fact_removal", "consolidation_update"),
    ),
    Commitment(
        "operational_escalation",
        "- Operational and account-wide requests",
        ("technical_issue_escalation", "full_data_deletion_escalation"),
    ),
    Commitment("consolidation", "- Consolidation:", ("consolidation_update",)),
    Commitment(
        "breadth_is_one_fact",
        "- Breadth is one fact, not many.",
        ("breadth_is_one_fact",),
    ),
    Commitment(
        "register_person_for_joining_only",
        "- `register_person` is for an unfamiliar sender",
        (
            "onboarding",
            "unfamiliar_sender_declines",
            "first_contact_explains_service",
        ),
    ),
    Commitment(
        "first_contact_judgment_call",
        "- First contact is a judgment call",
        ("first_contact_explains_service", "empty_body_first_contact"),
    ),
    Commitment(
        "joining_first_contact_reply_style",
        "- Joining first contact",
        ("onboarding",),
    ),
    Commitment(
        "outreach_timing_judgment_call",
        "- Outreach timing is a judgment call",
        ("onboarding",),
    ),
    Commitment(
        "search_person_id_is_not_sender",
        "- A `search` result's `person_id`",
        ("strong_match",),
    ),
    Commitment(
        "asking_for_clarification",
        "- Asking for clarification:",
        (
            "job_keyword_qualification",
            "underspecified_request",
            "vague_intent_qualification",
            "peer_level_qualification",
        ),
    ),
    Commitment(
        "not_every_message_is_career_request",
        "- Not every message is a career request.",
        ("multi_register_interests",),
    ),
    Commitment(
        "progressive_qualification_memory",
        "- Progressive qualification memory:",
        ("progressive_job_answer",),
    ),
    Commitment(
        "preferences_about_who",
        "- Preferences about who, not just what:",
        ("peer_level_qualification", "preference_mismatch"),
    ),
    Commitment(
        "preferences_about_who_proactive",
        "- Preferences about who, not just what:",
        ("proactive_preference_mismatch",),
    ),
    Commitment(
        "proactive_people_triggers",
        "- Proactive people triggers surface candidates",
        ("under_supported_proactive_people",),
    ),
    Commitment(
        "events_are_secondary",
        "- Events are secondary:",
        ("strong_event_relevance", "event_preference_mismatch"),
    ),
    Commitment(
        "event_records_vs_interests",
        "- Event records versus event interests:",
        (
            "one_off_event_submission",
            "recurring_event_submission",
            "nuanced_event_interest",
        ),
    ),
    Commitment(
        "proactive_event_triggers",
        "- Proactive event triggers:",
        ("strong_event_relevance", "event_preference_mismatch"),
    ),
    Commitment(
        "event_recommendation_permission",
        "- Event recommendation permission is separate",
        ("stop_event_recommendations_only", "resume_event_recommendations_only"),
    ),
)


adherence_dataset = Dataset[EmailScenario, RunOutcome](
    name="prompt_adherence",
    cases=[*archetype_dataset.cases, *_NEW_CASES],
)
ADHERENCE_CASES = tuple(adherence_dataset.cases)
ADHERENCE_CASE_IDS = tuple(case.name for case in ADHERENCE_CASES)


def _set_case_judge_model(
    adherence_case: Case[EmailScenario, RunOutcome, object], model: Any
) -> None:
    """Give every LLM evaluator in one case the same fresh provider client."""
    for evaluator in adherence_case.evaluators:
        if isinstance(evaluator, LLMJudge):
            evaluator.model = model


@dataclass
class AdherenceMeasurementRun:
    """Successful per-case reports collected during one pytest session."""

    reports: list[Any]

    @property
    def cases(self) -> list[Any]:
        return [case for report in self.reports for case in report.cases]

    def add(self, report: Any) -> None:
        self.reports.append(report)

    def is_complete(self) -> bool:
        return {case.name for case in self.cases} == set(ADHERENCE_CASE_IDS)


@pytest.fixture(scope="session")
def adherence_measurement_run(record_mode: str):
    """Aggregate a complete run without coupling the cases into one test.

    A targeted case or ``--lf`` retry intentionally produces no measurement.
    Once all cassettes exist, a full replay is cheap. Measurement writing is a
    separate opt-in so routine validation never dirties the repository.
    """
    run = AdherenceMeasurementRun(reports=[])
    yield run
    if (
        not run.is_complete()
        or os.environ.get("PROMPT_ADHERENCE_WRITE_MEASUREMENT") != "1"
    ):
        return

    settings = get_settings()
    baseline = load_baseline()
    rates = compute_commitment_adherence(SimpleNamespace(cases=run.cases))
    measurement = build_measurement(
        rates,
        model=settings.agent_model,
        commit=_current_commit_sha(),
        baseline=baseline,
        replayed=record_mode == "none",
    )
    record_measurement(MEASUREMENTS_PATH, measurement)
    print("\n" + format_comparison(measurement))


def compute_commitment_adherence(report: Any) -> dict[str, float]:
    """Per-commitment pass rate: mean, over its mapped case(s), of the
    fraction of that case's evaluator assertions that passed.

    A commitment mapped to several cases is scored across all of them so one
    lucky/unlucky run doesn't dominate; a commitment's case missing from the
    report (e.g. a partial re-run) contributes nothing rather than crashing.
    """
    cases_by_name = {case.name: case for case in report.cases}
    rates: dict[str, float] = {}
    for commitment in COMMITMENTS:
        case_rates: list[float] = []
        for case_name in commitment.case_names:
            case = cases_by_name.get(case_name)
            if case is None:
                continue
            values = [bool(assertion.value) for assertion in case.assertions.values()]
            if values:
                case_rates.append(sum(values) / len(values))
        rates[commitment.slug] = (
            sum(case_rates) / len(case_rates) if case_rates else 0.0
        )
    return rates


def _current_commit_sha() -> str:
    """The commit this measurement describes.

    `PROMPT_ADHERENCE_COMMIT` wins when set. A measurement is often run in an
    isolated box that holds a synced checkout but no `.git` directory, so
    `git rev-parse` there fails outright; the caller knows the commit and
    passes it in. Recording an unknown or wrong commit would make the record
    undiffable, which is the whole point of keeping one.
    """
    supplied = os.environ.get("PROMPT_ADHERENCE_COMMIT", "").strip()
    if supplied:
        return supplied
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    return result.stdout.strip()


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def compare_to_baseline(
    rates: dict[str, float], baseline: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Per-commitment delta of `rates` against a recorded baseline payload.

    Every commitment gets an entry with an explicit `status`, so a null result
    is reported as such rather than inferred from a missing key:

      - `measured` - the commitment existed at the baseline; `delta` is
        `current - baseline`, and `0.0` is a real, reportable null result.
      - `new_since_baseline` - the commitment has no baseline counterpart, so
        `delta` is `None`. Scoring it as a rise from zero would be a fiction:
        the bullet did not exist to be followed.
      - `dropped_since_baseline` - the baseline scored a commitment this
        harness no longer maps. Kept so a deletion stays visible in the record
        instead of silently shrinking the comparison.
    """
    baseline_rates = {
        slug: entry["rate"] for slug, entry in baseline["commitments"].items()
    }
    comparison: dict[str, dict[str, Any]] = {}
    for slug, rate in rates.items():
        if slug in baseline_rates:
            comparison[slug] = {
                "status": "measured",
                "baseline": baseline_rates[slug],
                "current": rate,
                "delta": rate - baseline_rates[slug],
            }
        else:
            comparison[slug] = {
                "status": "new_since_baseline",
                "baseline": None,
                "current": rate,
                "delta": None,
            }
    for slug, baseline_rate in baseline_rates.items():
        if slug not in rates:
            comparison[slug] = {
                "status": "dropped_since_baseline",
                "baseline": baseline_rate,
                "current": None,
                "delta": None,
            }
    return comparison


def build_measurement(
    rates: dict[str, float],
    *,
    model: str,
    commit: str,
    baseline: dict[str, Any],
    replayed: bool = False,
) -> dict[str, Any]:
    # Score the full commitment set, so a case missing from a partial report
    # lands as an explicit 0.0 rather than dropping out of the record.
    full_rates = {
        commitment.slug: rates.get(commitment.slug, 0.0) for commitment in COMMITMENTS
    }
    comparison = compare_to_baseline(full_rates, baseline)
    measured = [
        entry["delta"] for entry in comparison.values() if entry["status"] == "measured"
    ]
    return {
        "model": model,
        "commit": commit,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "prompt_shape": "per_mode",
        "replayed": replayed,
        "overall_rate": (
            (sum(full_rates.values()) / len(full_rates)) if full_rates else 0.0
        ),
        "baseline_commit": baseline["commit"],
        # Averaged over the commitments the baseline actually scored, so the
        # split variants added by this chain cannot flatter or drag the delta.
        "overall_delta_vs_baseline": (
            sum(measured) / len(measured) if measured else None
        ),
        "harness_change_note": HARNESS_CHANGE_NOTE,
        "commitments": {
            commitment.slug: {
                "prefix": commitment.prefix,
                "cases": list(commitment.case_names),
                "rate": rates.get(commitment.slug, 0.0),
                "confounded_by_harness_change": commitment.slug
                in CONFOUNDED_BY_HARNESS_CHANGE,
                **comparison[commitment.slug],
            }
            for commitment in COMMITMENTS
        },
        "dropped_since_baseline": sorted(
            slug
            for slug, entry in comparison.items()
            if entry["status"] == "dropped_since_baseline"
        ),
    }


def record_measurement(path: Path, measurement: dict[str, Any]) -> list[Any]:
    """Append `measurement` to the newest-last measurement log at `path`."""
    records = json.loads(path.read_text()) if path.exists() else []
    records.append(measurement)
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    return records


def format_comparison(measurement: dict[str, Any]) -> str:
    """Human-readable per-commitment comparison, printed by the live run."""
    lines = [
        f"prompt adherence: {measurement['model']} @ {measurement['commit'][:12]}",
        f"  overall {measurement['overall_rate']:.3f} "
        f"(baseline {measurement['baseline_commit'][:12]}, "
        f"mean delta {measurement['overall_delta_vs_baseline']})",
        f"  note: {measurement['harness_change_note']}",
    ]
    for slug, entry in sorted(measurement["commitments"].items()):
        suffix = (
            " [confounded by harness change]"
            if entry["confounded_by_harness_change"]
            else ""
        )
        if entry["status"] == "measured":
            lines.append(
                f"  {slug:<42} {entry['baseline']:.3f} -> "
                f"{entry['current']:.3f} ({entry['delta']:+.3f}){suffix}"
            )
        else:
            lines.append(
                f"  {slug:<42} {entry['status']}: {entry['current']:.3f}{suffix}"
            )
    for slug in measurement["dropped_since_baseline"]:
        lines.append(f"  {slug:<42} dropped since baseline")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Offline, fast structural guards (always run under `pytest -m "not integration"`)
# ---------------------------------------------------------------------------


def test_every_judgment_bullet_has_exactly_one_mapped_commitment() -> None:
    bullets_by_slug = {bullet.slug: bullet for bullet in JUDGMENT_BULLETS}
    commitment_slugs = [commitment.slug for commitment in COMMITMENTS]

    assert len(commitment_slugs) == len(set(commitment_slugs)), (
        "two Commitments share a slug - each judgment-notes bullet gets exactly one."
    )
    assert set(commitment_slugs) == set(bullets_by_slug), (
        "prompts.JUDGMENT_BULLETS and COMMITMENTS have drifted - update "
        "COMMITMENTS so each bullet has exactly one Commitment with its slug. "
        f"Unmapped: {sorted(set(bullets_by_slug) - set(commitment_slugs))}; "
        f"stale: {sorted(set(commitment_slugs) - set(bullets_by_slug))}"
    )

    for commitment in COMMITMENTS:
        bullet = bullets_by_slug[commitment.slug]
        assert bullet.text.startswith(commitment.prefix), (
            commitment.slug,
            commitment.prefix,
        )


def test_every_commitment_maps_to_at_least_one_real_case() -> None:
    case_names = {case.name for case in adherence_dataset.cases}
    for commitment in COMMITMENTS:
        assert commitment.case_names, commitment.slug
        for case_name in commitment.case_names:
            assert case_name in case_names, (commitment.slug, case_name)


def test_each_parametrized_case_can_receive_a_fresh_judge_model() -> None:
    evaluator = LLMJudge(rubric="test rubric", model="test:old")
    case = Case(name="judge_client", inputs=object(), evaluators=(evaluator,))
    fresh_model = object()

    _set_case_judge_model(case, fresh_model)

    assert evaluator.model is fresh_model


def test_key_free_replay_uses_only_an_internal_provider_placeholder() -> None:
    assert _provider_api_key("") == "cassette-replay-placeholder"
    assert _provider_api_key("configured-secret") == "configured-secret"


def test_scenario_record_snapshot_preserves_table_fields() -> None:
    snapshot = _snapshot_record(_strong_event)

    assert snapshot is not _strong_event
    assert snapshot.id == _strong_event.id
    assert snapshot.submitter_id == "user-event-owner"
    assert snapshot.text == "Private raw event submission"
    assert snapshot.gist == _strong_event.gist
    assert snapshot.expires_at == _strong_event.expires_at


def test_compute_commitment_adherence_averages_case_assertions() -> None:
    """Offline unit test of the scoring function against a fake report shape."""

    class _FakeAssertion:
        def __init__(self, value: bool) -> None:
            self.value = value

    class _FakeCase:
        def __init__(self, name: str, values: list[bool]) -> None:
            self.name = name
            self.assertions = {
                f"assertion_{i}": _FakeAssertion(v) for i, v in enumerate(values)
            }

    class _FakeReport:
        def __init__(self, cases: list[_FakeCase]) -> None:
            self.cases = cases

    report = _FakeReport(
        [
            _FakeCase("attachment_awareness", [True, True, False]),
            _FakeCase("job_keyword_qualification", [True]),
            _FakeCase("strong_match", [False, False]),
        ]
    )
    rates = compute_commitment_adherence(report)
    assert rates["attachments"] == pytest.approx(2 / 3)
    # search_similarity_discovery_only averages across two mapped cases
    assert rates["search_similarity_discovery_only"] == pytest.approx((1.0 + 0.0) / 2)
    # a commitment whose mapped case(s) are absent from the report scores 0.0
    assert rates["links"] == 0.0


_FAKE_BASELINE: dict[str, Any] = {
    "commit": "0" * 40,
    "commitments": {
        "kept_flat": {"rate": 0.5},
        "kept_risen": {"rate": 0.25},
        "gone": {"rate": 1.0},
    },
}


def test_compare_to_baseline_labels_flat_new_and_dropped_commitments() -> None:
    comparison = compare_to_baseline(
        {"kept_flat": 0.5, "kept_risen": 0.75, "added_later": 0.9},
        _FAKE_BASELINE,
    )

    # A null result is reported as a measured zero delta, not as a missing key.
    assert comparison["kept_flat"] == {
        "status": "measured",
        "baseline": 0.5,
        "current": 0.5,
        "delta": 0.0,
    }
    assert comparison["kept_risen"]["delta"] == pytest.approx(0.5)

    # A commitment that did not exist at the baseline is never scored as a
    # rise from zero - the bullet was not there to be followed.
    assert comparison["added_later"]["status"] == "new_since_baseline"
    assert comparison["added_later"]["baseline"] is None
    assert comparison["added_later"]["delta"] is None

    assert comparison["gone"]["status"] == "dropped_since_baseline"
    assert comparison["gone"]["current"] is None


def test_build_measurement_averages_the_delta_over_baseline_commitments_only() -> None:
    measurement = build_measurement(
        {slug: 1.0 for slug in (c.slug for c in COMMITMENTS)},
        model="test:model",
        commit="1" * 40,
        baseline=_FAKE_BASELINE,
    )
    # Only `kept_flat`/`kept_risen`/`gone` exist in the fake baseline, and none
    # of them is a real commitment slug, so nothing is comparable and the mean
    # delta is explicitly null rather than a misleading 0.0.
    assert measurement["overall_delta_vs_baseline"] is None
    assert measurement["dropped_since_baseline"] == ["gone", "kept_flat", "kept_risen"]
    assert measurement["prompt_shape"] == "per_mode"
    assert measurement["replayed"] is False
    assert measurement["baseline_commit"] == _FAKE_BASELINE["commit"]
    assert all(
        entry["status"] == "new_since_baseline"
        for entry in measurement["commitments"].values()
    )


def test_build_measurement_records_the_harness_change_caveat() -> None:
    measurement = build_measurement(
        {slug: 1.0 for slug in (c.slug for c in COMMITMENTS)},
        model="test:model",
        commit="1" * 40,
        baseline=_FAKE_BASELINE,
    )

    assert measurement["harness_change_note"] == HARNESS_CHANGE_NOTE

    for slug in CONFOUNDED_BY_HARNESS_CHANGE:
        assert measurement["commitments"][slug]["confounded_by_harness_change"] is True

    unflagged = next(
        commitment.slug
        for commitment in COMMITMENTS
        if commitment.slug not in CONFOUNDED_BY_HARNESS_CHANGE
    )
    assert (
        measurement["commitments"][unflagged]["confounded_by_harness_change"] is False
    )

    formatted = format_comparison(measurement)
    assert HARNESS_CHANGE_NOTE in formatted
    for slug in CONFOUNDED_BY_HARNESS_CHANGE:
        assert f"{slug:<42}" in formatted
    assert "[confounded by harness change]" in formatted


def test_build_measurement_marks_cassette_replay() -> None:
    measurement = build_measurement(
        {},
        model="test:model",
        commit="1" * 40,
        baseline=_FAKE_BASELINE,
        replayed=True,
    )
    assert measurement["replayed"] is True


def test_record_measurement_appends_and_never_targets_the_baseline(
    tmp_path: Path,
) -> None:
    assert MEASUREMENTS_PATH != BASELINE_PATH, (
        "the flat-prompt baseline is the fixed reference point; a run records "
        "a second measurement beside it rather than overwriting it."
    )

    path = tmp_path / "measurements.json"
    first = build_measurement({}, model="m", commit="a" * 40, baseline=_FAKE_BASELINE)
    second = build_measurement({}, model="m", commit="b" * 40, baseline=_FAKE_BASELINE)
    record_measurement(path, first)
    records = record_measurement(path, second)

    assert [record["commit"] for record in records] == ["a" * 40, "b" * 40]
    assert json.loads(path.read_text()) == records


def test_the_committed_baseline_is_readable_and_predates_the_split_bullets() -> None:
    baseline = load_baseline()
    recorded = set(baseline["commitments"])
    current = {commitment.slug for commitment in COMMITMENTS}

    assert recorded, "the flat-prompt baseline must stay in the repository"
    assert recorded <= current, (
        "the baseline scores commitments this harness no longer maps: "
        f"{sorted(recorded - current)}"
    )
    # The two commitments the baseline measured at 0.0 are the ones the live
    # comparison is required to call out, so they must still be comparable.
    for slug in (
        "progressive_qualification_memory",
        "sender_owned_evidence_memory_ids",
    ):
        assert baseline["commitments"][slug]["rate"] == 0.0


# ---------------------------------------------------------------------------
# The live per-case measurements
# ---------------------------------------------------------------------------


@pytest.fixture
def default_cassette_name(adherence_case: Case[EmailScenario, RunOutcome, object]):
    """Give each scenario a stable cassette independent of pytest node syntax."""
    return adherence_case.name


@pytest.mark.integration
@pytest.mark.live_model
@pytest.mark.vcr
@pytest.mark.block_network
@pytest.mark.asyncio
@pytest.mark.parametrize("adherence_case", ADHERENCE_CASES, ids=ADHERENCE_CASE_IDS)
async def test_prompt_adherence_case(
    adherence_case: Case[EmailScenario, RunOutcome, object],
    scenario_database,
    adherence_measurement_run: AdherenceMeasurementRun,
    successful_cassette_only,
    record_mode: str,
) -> None:
    """Run and score one scenario against the real configured model.

    Record all missing per-scenario cassettes deliberately:

        uv run pytest -s -m live_model --record-mode=once \
            tests/scenarios/test_prompt_adherence.py::test_prompt_adherence_case

    The default record mode is ``none``: it replays the committed cassette and
    fails on a miss rather than reaching the network. A failed case can be
    retried by its node id without re-recording successful cases.
    Set ``PROMPT_ADHERENCE_WRITE_MEASUREMENT=1`` on a complete run to append
    the aggregate comparison; ordinary replays have no repository side effect.
    """
    if record_mode != "none":
        _skip_without_credentials()
    settings = get_settings()
    baseline = load_baseline()
    assert settings.agent_model == baseline["model"], (
        "prompt-adherence recording must use the baseline model: "
        f"expected {baseline['model']!r}, got {settings.agent_model!r}"
    )
    judge_model = model_with_api_key(
        settings.test_llm_judge_model,
        _provider_api_key(settings.test_llm_judge_api_key),
        settings.model_request_timeout_seconds,
    )
    _set_case_judge_model(adherence_case, judge_model)
    case_dataset = Dataset[EmailScenario, RunOutcome](
        name=f"prompt_adherence_{adherence_case.name}",
        cases=[adherence_case],
    )
    report = await case_dataset.evaluate(
        partial(run_scenario, scenario_database=scenario_database),
        max_concurrency=1,
        retry_task=_retry_on_rate_limit(),
        retry_evaluators=_retry_on_rate_limit(),
    )

    # A case whose task raised never produced an observable action, so its
    # commitments would score 0.0 - indistinguishable in the record from a
    # model that ignored the bullet. Refuse to record a run that lost cases
    # to provider errors rather than publishing a fake regression.
    failures = getattr(report, "failures", [])
    assert not failures, (
        f"scenario {adherence_case.name!r} did not produce a measurement: "
        f"{[(failure.name, failure.error_message) for failure in failures]}"
    )
    assert len(report.cases) == 1
    evaluator_failures = [
        failure for case in report.cases for failure in case.evaluator_failures
    ] + report.report_evaluator_failures
    assert not evaluator_failures, (
        f"scenario {adherence_case.name!r} could not be scored: "
        f"{[failure.error_message for failure in evaluator_failures]}"
    )
    assert isinstance(report.cases[0].output, RunOutcome), (
        "scenario task was not awaited before evaluation"
    )
    adherence_measurement_run.add(report)
