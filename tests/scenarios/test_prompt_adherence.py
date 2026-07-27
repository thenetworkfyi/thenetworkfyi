"""Per-commitment adherence harness for SYSTEM_PROMPT's judgment-notes bullets.

`tests/test_prompts.py` proves each judgment-notes commitment is PRESENT in
`SYSTEM_PROMPT`'s text; it says nothing about whether the model actually FOLLOWS
one under realistic pressure. This module is that instrument: one
`pydantic_evals.Case` per commitment (reusing `tests/scenarios/test_live_archetypes.py`'s
dataset where a case already exercises a commitment, adding a case here where none
does), each scoring an observable action - a tool call, a reply's shape, a
memory's contents - never prompt-text presence.

Two offline, fast tests guard the harness's own shape and always run under
`pytest -m "not integration"`:
  - every bullet in `prompts.JUDGMENT_BULLETS` maps to exactly one
    `Commitment` here, matched by the bullet's stable slug, so neither
    reordering nor rewording a bullet breaks the mapping - only adding,
    splitting, or deleting one does, which is exactly the drift this guards
    against;
  - every `Commitment` names at least one real, collected `Case`.

The actual measurement - `test_prompt_adherence_baseline` - calls the real
configured `AGENT_MODEL`/`TEST_LLM_JUDGE_MODEL` the same way
`test_live_archetypes.py` does: marked `integration` + `live_model`, skipped
without credentials, DB/outbound mail mocked. Run it deliberately:

    uv run pytest -m live_model tests/scenarios/test_prompt_adherence.py::test_prompt_adherence_baseline

A successful run overwrites the committed baseline at `BASELINE_PATH` with the
per-commitment pass rate, the model, and the commit it was measured on, so
later prompt-restructuring tasks in this chain have something to diff against.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from thenetwork.agent.prompts import JUDGMENT_BULLETS
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
    _skip_without_credentials,
    archetype_dataset,
    run_scenario,
)

BASELINE_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "prompt-adherence-baseline.json"
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
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    return result.stdout.strip()


def write_baseline(
    path: Path, rates: dict[str, float], *, model: str, commit: str
) -> dict[str, Any]:
    payload = {
        "model": model,
        "commit": commit,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "overall_rate": (sum(rates.values()) / len(rates)) if rates else 0.0,
        "commitments": {
            commitment.slug: {
                "prefix": commitment.prefix,
                "cases": list(commitment.case_names),
                "rate": rates.get(commitment.slug, 0.0),
            }
            for commitment in COMMITMENTS
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


# ---------------------------------------------------------------------------
# Offline, fast structural guards (always run under `pytest -m "not integration"`)
# ---------------------------------------------------------------------------


def test_every_judgment_bullet_has_exactly_one_mapped_commitment() -> None:
    bullets_by_slug = {bullet.slug: bullet for bullet in JUDGMENT_BULLETS}
    commitment_slugs = [commitment.slug for commitment in COMMITMENTS]

    assert len(commitment_slugs) == len(set(commitment_slugs)), (
        "two Commitments share a slug - each judgment-notes bullet gets "
        "exactly one."
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


# ---------------------------------------------------------------------------
# The live measurement
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.live_model
@pytest.mark.asyncio
async def test_prompt_adherence_baseline() -> None:
    """Measure per-commitment adherence against the real AGENT_MODEL.

    Run deliberately - this overwrites the committed baseline at
    `BASELINE_PATH` so later prompt-restructuring tasks have something to
    diff against:

        uv run pytest -m live_model \
            tests/scenarios/test_prompt_adherence.py::test_prompt_adherence_baseline
    """
    _skip_without_credentials()
    settings = get_settings()
    report = await adherence_dataset.evaluate(run_scenario)
    rates = compute_commitment_adherence(report)
    payload = write_baseline(
        BASELINE_PATH,
        rates,
        model=settings.agent_model,
        commit=_current_commit_sha(),
    )
    assert payload["commitments"].keys() == {c.slug for c in COMMITMENTS}
