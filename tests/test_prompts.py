import asyncio
from functools import cache
import inspect
import re
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.prompts import (
    EVENT_TRIGGER,
    FIRST_CONTACT,
    KNOWN_SENDER,
    PEOPLE_TRIGGER,
    JUDGMENT_BULLETS,
    SYSTEM_PROMPTS,
)
from thenetwork.worker import proactive as proactive_module


def _bullet(slug: str):
    for bullet in JUDGMENT_BULLETS:
        if bullet.slug == slug:
            return bullet
    raise AssertionError(f"no judgment bullet with slug {slug!r}")


def _guidance(mode: str, slug: str) -> str:
    """The exact bullet text for `slug`, asserted present in `mode`'s composed prompt."""
    bullet = _bullet(slug)
    assert mode in bullet.modes, (mode, slug)
    assert bullet.text in SYSTEM_PROMPTS[mode]
    return " ".join(bullet.text.split())


def test_attachment_guidance_requires_an_accurate_sender_notice() -> None:
    guidance = _guidance(KNOWN_SENDER, "attachments")

    assert "`Attachments present but not read: N`" in guidance
    assert "attachment was not read" in guidance
    assert "via `reply_to_sender`" in guidance
    assert "paste any relevant content into the email" in guidance
    assert "When the line is absent, do not mention attachments" in guidance
    for inaccurate in ("removed", "stripped", "deleted"):
        assert inaccurate not in guidance.lower()


def test_link_guidance_does_not_imply_fetch_capability() -> None:
    guidance = _guidance(KNOWN_SENDER, "links")

    assert "cannot open links or read what is behind them" in guidance
    assert "visible reference, not as page content" in guidance
    assert "do not infer the destination's contents" in guidance
    assert "ask the sender what is behind the link" in guidance
    assert "trailing `…`" in guidance
    assert "incomplete reference" in guidance


def test_agent_email_tools_require_content_free_sent_summary() -> None:
    for mode in (FIRST_CONTACT, KNOWN_SENDER):
        prompt = SYSTEM_PROMPTS[mode]
        assert "Every `reply_to_sender` or `send_outreach` call" in prompt
        assert "`sent_email_summary`" in prompt
        assert "Do not copy the subject or body" in prompt
        assert "include an address or headers" in prompt


def test_sent_email_summary_is_absent_from_proactive_modes() -> None:
    for mode in (PEOPLE_TRIGGER, EVENT_TRIGGER):
        assert "sent_email_summary" not in SYSTEM_PROMPTS[mode]


def test_possible_match_guidance_describes_current_email_capability() -> None:
    prompt = SYSTEM_PROMPTS[KNOWN_SENDER]
    intro_guidance = prompt.split("- A possible match:", 1)[1].split(
        "- A one-way share", 1
    )[0]
    intro_guidance = " ".join(intro_guidance.split())

    assert "`propose_introduction`" in intro_guidance
    assert "no names or contact details" in intro_guidance
    assert "Only after both reply yes" in intro_guidance
    assert "you cannot assert consent" in intro_guidance
    assert "Never use `send_outreach` to work around this flow" in intro_guidance
    assert "consent-like reply without an `[intro:...]` token" in intro_guidance
    assert "copy the token string into their reply" in intro_guidance


def test_match_guidance_treats_similarity_as_discovery_only() -> None:
    guidance = _guidance(KNOWN_SENDER, "search_similarity_discovery_only")

    assert "candidate discovery, not a fit score" in guidance
    assert "specific two-sided match thesis" in guidance
    assert "what each person is seeking" in guidance
    assert "what the other could materially contribute" in guidance
    assert "shared keyword, tool, title, topic, or city" in guidance
    assert "Missing or contradictory evidence" in guidance
    assert "act without interrogating" in guidance


def test_match_guidance_treats_a_closing_window_as_consequential_evidence() -> None:
    guidance = _guidance(KNOWN_SENDER, "search_similarity_discovery_only")

    assert "sender-stated closing window" in guidance
    assert "itself consequential match evidence" in guidance
    assert "prefer proposing within the window" in guidance
    assert "qualifying further or waiting until the opportunity expires" in guidance
    assert "Urgency may lower the fit bar" in guidance
    assert "never the two-sided-thesis bar" in guidance
    assert "in town this week" in guidance
    assert "want to meet people" in guidance
    assert "shared keyword or city without stated intent is still no thesis" in guidance


def test_sender_owned_group_is_the_only_search_evidence_with_memory_ids() -> None:
    guidance = _guidance(KNOWN_SENDER, "sender_owned_evidence_memory_ids")

    assert "groups the sender's own evidence" in guidance
    assert "Only its evidence items carry `memory_id`" in guidance
    assert "Cross-user evidence items contain sealed `gist` only" in guidance
    assert "Never pass the sender-owned candidate's `person_id`" in guidance


def test_thin_standing_intent_guidance_requests_one_material_follow_up() -> None:
    guidance = _guidance(KNOWN_SENDER, "asking_for_clarification")

    assert "broad or concrete-but-thin" in guidance
    assert "mostly use React" in guidance
    assert "target level, role scope, or demonstrated React experience" in guidance
    assert "exactly one brief, neutral, high-information question" in guidance
    assert "Across jobs, collaborators, peers, mentors, founders" in guidance
    assert "not for generic profile completeness" in guidance
    assert "This is a qualification turn" in guidance
    assert "do not call `propose_introduction`" in guidance
    assert "even if `search` found a semantically adjacent person" in guidance
    assert "Do not interrogate every message" in guidance
    assert "exactly one question mark" in guidance
    assert "one evidence category" in guidance
    assert "role and hands-on evidence" in guidance
    assert "do not collapse those stages" in guidance
    assert "`remember` that you asked" in guidance


def test_a_passive_matching_promise_cannot_stand_in_for_a_question() -> None:
    """The observed failure is a warm acknowledgment that asks nothing.

    Production runs showed the agent closing an underspecified request with a
    promise to keep the sender in mind and zero question marks, which leaves
    the next run starting exactly where the last one did. The prohibition is
    stated as a check on the outgoing reply rather than as a general principle,
    because the model has to be able to apply it to the text it just wrote.
    """
    guidance = _guidance(KNOWN_SENDER, "asking_for_clarification")

    assert (
        "Before sending a reply to an unsupported request for a connection" in guidance
    )
    assert "check the reply itself" in guidance
    assert "must contain exactly one question mark" in guidance
    assert "must not say you will keep the sender in mind" in guidance
    assert "watch for someone suitable" in guidance
    assert "reach out when a match appears" in guidance
    assert "defers the work to a later turn you cannot schedule" in guidance
    assert "That sentence is the substitution to watch for" in guidance
    assert "asks nothing" in guidance
    assert "the next run starts exactly where this one did" in guidance
    assert (
        "Acknowledging without asking, `no_action`, and a promise to keep looking "
        "are all the same failure" in guidance
    )


def test_multi_register_interests_are_not_collapsed_into_a_career_request() -> None:
    guidance = _guidance(KNOWN_SENDER, "not_every_message_is_career_request")

    assert "more than one register" in guidance
    assert "a real thread, not background around the professional one" in guidance
    assert "same specificity you would give a job title" in guidance
    assert "must not leave only the employable one in memory" in guidance
    assert "ask about the thread the sender put weight on" in guidance
    assert "defaulting to the career one" in guidance
    assert "is not by itself an ask" in guidance
    assert "never said they were looking for work" in guidance
    assert "legitimate basis for an introduction on its own terms" in guidance
    assert "not a lower bar, and not a higher one" in guidance


def test_breadth_is_remembered_once_rather_than_per_claimed_field() -> None:
    """A rotating list of unrelated fields is one fact about the ask's breadth.

    Rotating claims never supersede one another, so the consolidation guidance
    keyed on `consolidation_candidates` cannot catch them; without this the
    notes accumulate one per claimed label.
    """
    guidance = _guidance(KNOWN_SENDER, "breadth_is_one_fact")

    assert "their ask is broad" in guidance
    assert "a single standing note" in guidance
    assert "rather than a separate durable fact per named field" in guidance
    assert "will not catch them" in guidance
    assert "is not evidence of an interest" in guidance
    assert "instead of banking each new label" in guidance


def test_match_thesis_constraints_are_not_only_professional() -> None:
    guidance = _guidance(KNOWN_SENDER, "search_similarity_discovery_only")

    assert "Consequential constraints are not only professional" in guidance
    assert "level, scene, locality, format" in guidance


def test_progressive_qualification_replaces_one_enriched_intent_note() -> None:
    guidance = _guidance(KNOWN_SENDER, "progressive_qualification_memory")

    assert "answer closes only the gap it actually answers" in guidance
    assert "Preserve earlier material context and constraints" in guidance
    assert "one small enriched note using `forget` + `remember`" in guidance
    assert "forget the answered asked-note" in guidance
    assert "Do not accumulate a trail of partial intent notes" in guidance
    assert "before reconsidering a match" in guidance
    assert "ask one next question" in guidance
    assert "Carry every material fact from the old standing gist" in guidance
    assert "forget the old standing note and answered asked-note" in guidance
    assert "exactly one consolidated standing intent" in guidance
    assert "remember exactly one new asked-note" in guidance
    assert "do not propose" in guidance
    assert "supports both what the sender brings" in guidance


def test_progressive_qualification_memory_is_absent_before_registration() -> None:
    """Reachable only once a standing-intent note can exist, i.e. after registration."""
    bullet = _bullet("progressive_qualification_memory")
    assert bullet.modes == frozenset({KNOWN_SENDER})
    assert bullet.text not in SYSTEM_PROMPTS[FIRST_CONTACT]


def test_status_vocabulary_guidance_present() -> None:
    bullet = _bullet("tool_status_vocabulary")
    assert bullet.modes == frozenset({FIRST_CONTACT, KNOWN_SENDER})
    for mode in bullet.modes:
        assert bullet.text in SYSTEM_PROMPTS[mode]

    guidance = " ".join(bullet.text.split())
    assert "tools never crash" in guidance
    assert "`limited` or `deferred`" in guidance
    assert "do not retry the same tool call" in guidance
    assert "`forbidden` means the action is structurally disallowed" in guidance
    assert "never work around it" in guidance
    assert "`error` with a `reason` means fix the input once" in guidance
    assert "never loop on the same error" in guidance
    # The interactive recovery routes - only reachable where they are registered.
    assert "via `reply_to_sender`" in guidance
    assert "or escalate" in guidance


def test_status_vocabulary_has_a_proactive_variant_with_no_reply_or_escalate() -> None:
    """The commitment is kept for both proactive modes, not deleted from them.

    The flat bullet told every mode to answer a fired cap "briefly via
    `reply_to_sender`" and to "escalate" on a repeated error. Neither tool is
    registered on a proactive trigger, so that text instructed an action the
    model structurally could not take; `no_action(reason)` is the only way a
    trigger run can end once its one bound capability is unusable.
    """
    bullet = _bullet("tool_status_vocabulary_proactive")
    assert bullet.modes == frozenset({PEOPLE_TRIGGER, EVENT_TRIGGER})
    guidance = " ".join(bullet.text.split())

    # The shared commitment survives verbatim.
    assert "tools never crash" in guidance
    assert "`limited` or `deferred`" in guidance
    assert "do not retry the same tool call" in guidance
    assert "`forbidden` means the action is structurally disallowed" in guidance
    assert "never work around it" in guidance
    assert "never loop on the same error" in guidance

    # Redirected to the only terminal action a trigger run actually has.
    assert "`no_action(reason)`" in guidance
    assert "no reply and no escalation path" in guidance
    assert "reply_to_sender" not in guidance
    assert "escalate" not in guidance


def test_operational_and_account_wide_requests_escalate() -> None:
    guidance = _guidance(KNOWN_SENDER, "operational_escalation")

    assert "broken or not working as expected" in guidance
    assert "complaint that needs follow-up" in guidance
    assert "delete their account or all data" in guidance
    assert "call `escalate(reason)`" in guidance
    assert "Do not attempt remediation" in guidance
    assert "call `forget`" in guidance
    assert "request to forget or correct one or more specific" in guidance
    assert "should use `forget`" in guidance


def test_consolidation_guidance_present() -> None:
    guidance = _guidance(KNOWN_SENDER, "consolidation")

    assert "`consolidation_candidates`" in guidance
    assert "stale version of the fact you just saved" in guidance
    assert "`forget` the stale one (edit = forget + remember" in guidance
    assert "Do not forget a candidate that is merely related" in guidance
    assert "Only a memory solely about the sender can be forgotten" in guidance
    assert "co-owned (multi-ref) memory is protected" in guidance
    assert "`status=forbidden`" in guidance


def test_register_person_exists_guidance_present() -> None:
    guidance = _guidance(FIRST_CONTACT, "register_person_for_joining_only")

    assert "`status=exists`" in guidance
    assert "use the returned id and continue normally" in guidance
    assert "this is not a failure" in guidance
    assert "does not need" in guidance and "retry" in guidance


def test_unknown_sender_answer_and_opt_out_escalation_guidance() -> None:
    guidance = _guidance(FIRST_CONTACT, "register_person_for_joining_only")

    assert "only asking what the service does" in guidance
    assert (
        "explicitly decline participation, data retention, or further email" in guidance
    )
    assert "call `escalate`" in guidance
    assert "without registering them, saving a memory, or replying" in guidance
    # This phrasing lives in the neighboring `search_person_id_is_not_sender`
    # bullet, which also reaches first_contact mode.
    assert "authenticated inbound address without registering" in _guidance(
        FIRST_CONTACT, "search_person_id_is_not_sender"
    )


def test_register_person_guidance_is_absent_from_known_sender_mode() -> None:
    """A known sender is already registered - register_person cannot fire again."""
    bullet = _bullet("register_person_for_joining_only")
    assert bullet.modes == frozenset({FIRST_CONTACT})
    assert bullet.text not in SYSTEM_PROMPTS[KNOWN_SENDER]


def test_first_contact_is_model_directed_and_uses_plain_public_language() -> None:
    guidance = _guidance(FIRST_CONTACT, "first_contact_judgment_call")

    assert "not a character-count rule" in guidance
    assert "`send_first_contact_welcome`" in guidance
    assert "without registering the sender or notifying an operator" in guidance
    assert "answer the actual question with `reply_to_sender`" in guidance
    assert "asks both people" in guidance
    assert '"autonomous connector,"' in guidance
    assert '"profile database,"' in guidance
    assert '"two-sided match thesis"' in guidance
    assert "internal design terms, not product copy" in guidance
    assert "do not send both" in guidance


def test_joining_first_contact_reply_style_guidance_present() -> None:
    guidance = _guidance(FIRST_CONTACT, "joining_first_contact_reply_style")

    assert "register and remember what the sender shared" in guidance
    assert "not a confirmation form" in guidance
    assert "noted that you're X, Y, and Z" in guidance
    assert "anonymized sketch" in guidance


def test_outreach_timing_guidance_present() -> None:
    bullet = _bullet("outreach_timing_judgment_call")
    assert bullet.modes == frozenset({FIRST_CONTACT, KNOWN_SENDER})
    guidance = " ".join(bullet.text.split())

    assert "judgment call, not a line to recite" in guidance
    assert "never more than once per sender" in guidance
    assert "Never promise a match or a timeline" in guidance


def test_search_person_id_guidance_present() -> None:
    guidance = _guidance(KNOWN_SENDER, "search_person_id_is_not_sender")

    assert "identifies whoever that memory is about" in guidance
    assert "never the current sender" in guidance
    assert "accepts no recipient ID" in guidance


def _event_guidance(mode: str, slug: str) -> str:
    return _guidance(mode, slug)


def test_event_guidance_keeps_events_secondary_and_relevance_separate() -> None:
    guidance = _event_guidance(KNOWN_SENDER, "events_are_secondary")

    assert "core value is making unusually relevant people connections" in guidance
    assert "Judge event relevance separately" in guidance
    assert "strong people match does not make an event relevant" in guidance
    assert "relevant event is never a reason to call `propose_introduction`" in guidance


def test_event_submission_and_interest_guidance_preserves_freeform_meaning() -> None:
    guidance = _event_guidance(KNOWN_SENDER, "event_records_vs_interests")

    assert "record the event with `create_event`, not `remember`" in guidance
    assert "one event record for a one-off event" in guidance
    assert "one event record with `recurrence` for a recurring series" in guidance
    assert (
        "what events a person wants to hear about belongs in ordinary person memory"
        in guidance
    )
    assert (
        "topic, format, location, audience or experience level, and timing" in guidance
    )
    assert "Do not flatten a specific preference into a generic topic" in guidance


def test_event_trigger_uses_only_server_composed_capability() -> None:
    guidance = _event_guidance(EVENT_TRIGGER, "proactive_event_triggers")

    assert "Compare those gists carefully" in guidance
    assert "High semantic similarity is not enough" in guidance
    assert "call `send_event_recommendation` with the trigger's event id" in guidance
    assert "composes the concise FYI server-side" in guidance
    assert (
        "never use `send_outreach`, `reply_to_sender`, or model-written copy"
        in guidance
    )
    assert "never call `propose_introduction` during an event trigger" in guidance


def test_proactive_event_triggers_guidance_is_event_trigger_exclusive() -> None:
    bullet = _bullet("proactive_event_triggers")
    assert bullet.modes == frozenset({EVENT_TRIGGER})
    for mode in (PEOPLE_TRIGGER, FIRST_CONTACT, KNOWN_SENDER):
        assert bullet.text not in SYSTEM_PROMPTS[mode]


def test_event_permission_is_scoped_without_service_promises() -> None:
    guidance = _event_guidance(KNOWN_SENDER, "event_recommendation_permission")

    assert "opt out of event recommendations by saying no" in guidance
    assert "`stop_event_recommendations`" in guidance
    assert "`resume_event_recommendations`" in guidance
    assert "Never describe this as opting out of people recommendations" in guidance
    assert "introduction consent stays pair-specific" in guidance
    assert "Do not use `remember` or `forget` as the enforcement state" in guidance
    for unsupported in (
        "reminders",
        "RSVP handling",
        "attendance tracking",
        "post-event follow-up",
        "calendar management",
    ):
        assert unsupported in guidance


def test_preferences_about_who_reaches_search_and_proactive_people_matches() -> None:
    """Explicitly names both a `search` hit and a proactive trigger as contexts."""
    bullet = _bullet("preferences_about_who")
    assert bullet.modes == frozenset({KNOWN_SENDER})
    guidance = " ".join(bullet.text.split())

    assert "part of the match, not decoration" in guidance
    assert "a `search` hit or a proactive trigger" in guidance
    assert "treat a stated preference as a constraint" in guidance
    assert "not license to assume it holds" in guidance
    # The write side is reachable here because `remember` is registered.
    assert "`remember` it in their own terms" in guidance


def test_preferences_about_who_has_a_people_trigger_variant_that_only_judges() -> None:
    """The constraint half is kept for `people_trigger`; the write half is not.

    The flat bullet told `people_trigger` to "`remember` it in their own terms"
    and to weigh "a `search` hit", but that mode registers neither `remember`
    nor `search` - only `propose_introduction` and `no_action`. The variant
    keeps the judgment commitment, which the trigger can act on, and drops the
    two instructions it cannot.
    """
    bullet = _bullet("preferences_about_who_proactive")
    assert bullet.modes == frozenset({PEOPLE_TRIGGER})
    guidance = " ".join(bullet.text.split())

    assert "part of the match, not decoration" in guidance
    assert "Treat it as a constraint on this trigger" in guidance
    assert "contradicts the preference" in guidance
    assert "not license to assume it holds" in guidance
    assert "call `no_action`" in guidance
    assert "`remember`" not in guidance
    assert "`search`" not in guidance


# ---------------------------------------------------------------------------
# The "Proactive people triggers" bullet was checked for the duplication this
# chain's acceptance criteria call out - see the comment above JUDGMENT_BULLETS
# for the finding: `scan_for_matches`'s trigger body restates this reasoning,
# but `scan_for_opportunities`'s does not, so the bullet is kept (not deleted)
# for `people_trigger` mode. The event-trigger bullet gets the same check and
# comes out the other way: neither event scan body duplicates it.
# ---------------------------------------------------------------------------


def test_under_supported_proactive_people_candidate_is_no_action() -> None:
    guidance = _guidance(PEOPLE_TRIGGER, "proactive_people_triggers")

    assert "surface candidates; they do not establish fit" in guidance
    assert "same two-sided thesis" in guidance
    assert "call `no_action`" in guidance
    assert "Do not call `propose_introduction`" in guidance
    assert "under-supported proactive candidate" in guidance


def test_scan_for_matches_body_duplicates_the_proactive_people_triggers_bullet() -> (
    None
):
    """Only the semantic-rematch scan restates the bullet's reasoning inline."""
    source = inspect.getsource(proactive_module.scan_for_matches)
    assert "two-sided" in source
    assert "materially supported common ground" in source
    assert "propose_introduction" in source
    assert "no_action" in source


def test_scan_for_opportunities_body_does_not_duplicate_the_bullet() -> None:
    """The graph-proximity scan carries no thesis/no_action reasoning at all -
    this is why the bullet is kept rather than deleted for `people_trigger`."""
    source = inspect.getsource(proactive_module.scan_for_opportunities)
    assert "two-sided" not in source
    assert "no_action" not in source


def test_event_scan_trigger_body_does_not_duplicate_the_prompt_bullet() -> None:
    """Unlike `scan_for_matches`, `event_scan.py`'s trigger body is a short
    pointer to the sealed gists ("Judge relevance only from these sanitized
    gists"); it does not restate the "Compare those gists carefully...
    constraint conflicts" reasoning that `proactive_event_triggers` carries,
    so that bullet is kept for `event_trigger` mode for the same underlying
    reason `proactive_people_triggers` is kept.
    """
    from thenetwork.worker import event_scan as event_scan_module

    source = inspect.getsource(event_scan_module.scan_for_event_recommendations)
    assert "Compare those gists carefully" not in source
    assert "High semantic similarity is not enough" not in source


# ---------------------------------------------------------------------------
# Structural cross-check: a bullet appears in a mode's composed prompt if and
# only if that mode is in its declared `modes` set.
# ---------------------------------------------------------------------------


# Exact per-mode bullet membership. This is the drift alarm for the tagging
# itself: adding a bullet, retagging one, or splitting one into an interactive
# and a proactive variant all have to be recorded here deliberately. The size
# bounds below count the same sets.
_EXPECTED_BULLETS_BY_MODE: dict[str, frozenset[str]] = {
    PEOPLE_TRIGGER: frozenset(
        {
            "tool_status_vocabulary_proactive",
            "preferences_about_who_proactive",
            "proactive_people_triggers",
        }
    ),
    EVENT_TRIGGER: frozenset(
        {
            "tool_status_vocabulary_proactive",
            "proactive_event_triggers",
        }
    ),
    FIRST_CONTACT: frozenset(
        {
            "attachments",
            "links",
            "search_similarity_discovery_only",
            "sender_owned_evidence_memory_ids",
            "tool_status_vocabulary",
            "forget_ownership",
            "operational_escalation",
            "consolidation",
            "breadth_is_one_fact",
            "register_person_for_joining_only",
            "first_contact_judgment_call",
            "joining_first_contact_reply_style",
            "outreach_timing_judgment_call",
            "search_person_id_is_not_sender",
            "not_every_message_is_career_request",
            "events_are_secondary",
            "event_records_vs_interests",
            "event_recommendation_permission",
            "asking_for_clarification",
        }
    ),
    KNOWN_SENDER: frozenset(
        {
            "attachments",
            "links",
            "search_similarity_discovery_only",
            "sender_owned_evidence_memory_ids",
            "tool_status_vocabulary",
            "forget_ownership",
            "operational_escalation",
            "consolidation",
            "breadth_is_one_fact",
            "outreach_timing_judgment_call",
            "search_person_id_is_not_sender",
            "not_every_message_is_career_request",
            "preferences_about_who",
            "events_are_secondary",
            "event_records_vs_interests",
            "event_recommendation_permission",
            "asking_for_clarification",
            "progressive_qualification_memory",
        }
    ),
}


def test_per_mode_bullet_membership_is_pinned() -> None:
    for mode, expected in _EXPECTED_BULLETS_BY_MODE.items():
        actual = frozenset(
            bullet.slug for bullet in JUDGMENT_BULLETS if mode in bullet.modes
        )
        assert actual == expected, (mode, sorted(actual ^ expected))


def test_every_bullet_slug_is_unique_and_reaches_at_least_one_mode() -> None:
    slugs = [bullet.slug for bullet in JUDGMENT_BULLETS]
    assert len(slugs) == len(set(slugs))
    for bullet in JUDGMENT_BULLETS:
        assert bullet.modes, bullet.slug
        assert bullet.modes <= frozenset(SYSTEM_PROMPTS), bullet.slug


# The build_agent arguments that put a run in each mode. These mirror
# `prompts.system_prompt_for`'s own branch, which is the point: the tool set
# below is read out of the agent `build_agent` actually constructs for these
# arguments, so `core.py` stays the single source of truth and the two
# mode-selection branches are exercised against each other.
_MODE_BUILD_KWARGS: dict[str, dict[str, Any]] = {
    PEOPLE_TRIGGER: {
        "is_proactive": True,
        "proactive_candidate_id": "person-under-test",
    },
    EVENT_TRIGGER: {"is_proactive": True, "proactive_event_id": "event-under-test"},
    FIRST_CONTACT: {"sender_known": False},
    KNOWN_SENDER: {"sender_known": True},
}


@cache
def _registered_tools(mode: str) -> frozenset[str]:
    """Tool names a run in `mode` can actually call, read from the built agent.

    Restating these by hand is how the check below silently becomes a no-op:
    a tool added, removed, or moved between the branches of `build_agent`
    leaves a hand-written copy stale with nothing to detect the drift, and the
    prompts then get validated against a list that no longer describes any
    real run. Running the agent is the only way to ask it what it registered.

    `no_action` arrives via `info.output_tools` rather than `info.function_tools`
    because it is the output tool, so both are unioned instead of adding its
    name back by hand.
    """
    observed: set[str] = set()

    async def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        observed.update(tool.name for tool in info.function_tools)
        observed.update(tool.name for tool in info.output_tools)
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name="no_action", args={"reason": "probe"}),
            ]
        )

    agent = build_agent(model=FunctionModel(capture), **_MODE_BUILD_KWARGS[mode])
    asyncio.run(agent.run("probe", deps=AgentDeps()))
    return frozenset(observed)


_ALL_TOOLS = frozenset().union(*(_registered_tools(mode) for mode in SYSTEM_PROMPTS))

# A clause that forbids a tool may name one the mode does not register - that
# is the point of "never call `send_outreach`". A clause that does anything
# else with an unregistered tool name is the bug this test exists to catch.
_PROHIBITION_MARKERS = ("never", "not ", "no ", "cannot", "instead of")


def _clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"(?<=[.;:])\s+|\n", text)
        if clause.strip()
    ]


def test_no_mode_prompt_instructs_a_tool_that_mode_does_not_register() -> None:
    """The regression this whole task is about, checked mechanically.

    Reaching a mode with guidance about a tool it cannot call is not a harmless
    extra sentence: the model is being told to take an action that will not be
    available, and its only ways out are inventing a call or ending the run on
    bare text. Prompts name tools in backticks, so unbackticked prose ("search
    for relevant memories") is deliberately not matched.
    """
    offences = {
        mode: _unregistered_instructions(mode, prompt)
        for mode, prompt in SYSTEM_PROMPTS.items()
    }
    assert not any(offences.values()), {k: v for k, v in offences.items() if v}


def _unregistered_instructions(mode: str, text: str) -> list[str]:
    registered = _registered_tools(mode)
    found = []
    for clause in _clauses(text):
        named = {tool for tool in _ALL_TOOLS if f"`{tool}" in clause}
        if not (named - registered):
            continue
        if any(marker in clause.lower() for marker in _PROHIBITION_MARKERS):
            continue
        found.append(clause)
    return found


def test_the_derived_tool_sets_are_populated_and_mode_specific() -> None:
    """Guards the derivation: empty sets would make the detector vacuous.

    `_unregistered_instructions` can only flag a clause that names a tool in
    `_ALL_TOOLS`. If the probe run silently registered nothing, that union is
    empty, no clause ever matches, and the check above passes while inspecting
    nothing - the same silent no-op this task removed the hand-written list to
    avoid. Pin the shape of what `build_agent` returns, not just that it ran.
    """
    for mode in SYSTEM_PROMPTS:
        assert _registered_tools(mode), mode
        # The output tool reaches every mode and must come from `output_tools`.
        assert "no_action" in _registered_tools(mode), mode

    assert "propose_introduction" in _registered_tools(PEOPLE_TRIGGER)
    assert "send_event_recommendation" in _registered_tools(EVENT_TRIGGER)

    # The proactive modes are capability grants for exactly one bound action.
    for mode in (PEOPLE_TRIGGER, EVENT_TRIGGER):
        assert len(_registered_tools(mode)) == 2, (mode, _registered_tools(mode))

    # `send_event_recommendation` is withheld from ordinary inbound runs; the
    # security-boundary text for the interactive modes depends on that.
    for mode in (FIRST_CONTACT, KNOWN_SENDER):
        assert "reply_to_sender" in _registered_tools(mode), mode
        assert "send_event_recommendation" not in _registered_tools(mode), mode


def test_the_unregistered_tool_detector_flags_the_original_regression() -> None:
    """Guards the guard: a detector that cannot fail proves nothing.

    These are the two exact clauses this task removed from the proactive
    prompts. If a future edit to `_PROHIBITION_MARKERS` or `_clauses` stops
    matching them, the check above silently becomes a no-op.
    """
    removed_cap_clause = (
        "If the cap blocks what the sender actually asked for, say so briefly "
        "via `reply_to_sender`; otherwise just capture the fact and move on."
    )
    removed_remember_clause = (
        "`remember` it in their own terms as part of their standing intent so "
        "it carries into matching."
    )

    assert _unregistered_instructions(PEOPLE_TRIGGER, removed_cap_clause)
    assert _unregistered_instructions(EVENT_TRIGGER, removed_cap_clause)
    assert _unregistered_instructions(PEOPLE_TRIGGER, removed_remember_clause)

    # ...and that it stays quiet for a legitimate prohibition and for a tool
    # the mode actually registers.
    assert not _unregistered_instructions(
        PEOPLE_TRIGGER, "Never use `send_outreach` to deliver this."
    )
    assert not _unregistered_instructions(
        KNOWN_SENDER, "Answer them with `reply_to_sender`."
    )


def test_every_mode_ends_with_a_security_boundaries_section() -> None:
    """Each mode's untrusted-content text points at "the security boundaries
    below"; `people_trigger` previously had none, leaving that a dangling
    reference in the one mode most exposed to sealed-gist injection."""
    heading = "Security boundaries (structural, not policy):"
    for mode, prompt in SYSTEM_PROMPTS.items():
        assert "bypass the security boundaries below" in prompt, mode
        assert prompt.count(heading) == 1, mode
        # "below" is only true if the section is genuinely the last block.
        assert prompt.rstrip().split("\n\n")[-1].startswith(heading), mode


def test_people_trigger_security_boundary_covers_its_one_capability() -> None:
    prompt = SYSTEM_PROMPTS[PEOPLE_TRIGGER]
    boundaries = prompt.rsplit("Security boundaries (structural, not policy):", 1)[1]

    assert "`propose_introduction` accepts only the opaque person ids" in boundaries
    assert "sealed gists you supply" in boundaries
    assert "raw memory text, name, or email address" in boundaries
    assert "server composes and sends the anonymized opt-in requests" in boundaries


def test_every_bullet_appears_only_in_its_declared_modes() -> None:
    for bullet in JUDGMENT_BULLETS:
        for mode, prompt in SYSTEM_PROMPTS.items():
            present = bullet.text in prompt
            expected = mode in bullet.modes
            assert present == expected, (bullet.slug, mode, present, expected)


def test_send_event_recommendation_is_named_only_where_it_is_registered() -> None:
    """A prior bug: the tools line named `send_event_recommendation` even in
    modes where it is not registered (only `event_trigger` registers it)."""
    for mode in (PEOPLE_TRIGGER, FIRST_CONTACT, KNOWN_SENDER):
        assert "send_event_recommendation" not in SYSTEM_PROMPTS[mode]
    assert "send_event_recommendation" in SYSTEM_PROMPTS[EVENT_TRIGGER]


# The production model is a 31B instruct model, so the binding constraint on
# each mode's prompt is instruction adherence across a long system message,
# not context capacity - the window is far larger than anything here. There is
# no published size cliff to point at, so these bounds are drift alarms rather
# than targets, and the only correct response to a breach is consolidating
# overlapping guidance. Never buy headroom by deleting a behavioral
# commitment: every one of them is pinned by a test above precisely so that
# shortcut fails loudly.
#
# Measured the same way at every point of comparison (rendered composed
# prompt text via Python, never `wc -c` on prompts.py - the source file's
# internal structure, including this module's docstrings and dict literals,
# never reaches the model):
#
#   08f8114 (before the prompt-adjustments project): 22493 chars, 20 bullets,
#     median bullet 725, mean 816, longest 1958 (single flat prompt)
#   eff77d6 (round 1 assembled):                     22871 chars, 16 bullets,
#     median 797, mean 1054, longest 2669 (single flat prompt)
#   12c6e20f (per-mode assembly, 23 bullets total - none deleted; see
#     "proactive_people_triggers" below, which was considered for removal as a
#     duplicate of worker/proactive.py's trigger body but kept because only
#     `scan_for_matches`'s body actually restates it, not `scan_for_opportunities`'s):
#     known_sender:    18846 chars, 18 bullets reach this mode
#     first_contact:   19603 chars, 19 bullets reach this mode
#     people_trigger:   4434 chars,  3 bullets reach this mode
#     event_trigger:    3957 chars,  2 bullets reach this mode
#   this task (25 bullets total; the two proactive modes stopped being told to
#     use tools they do not register). `tool_status_vocabulary` and
#     `preferences_about_who` each split into an interactive bullet plus a
#     proactive variant, so the total rose by 2 while no mode's bullet count
#     changed. `people_trigger` also gained a security-boundaries section,
#     which its untrusted-content text already referred to:
#     known_sender:    18846 chars, 18 bullets reach this mode  (unchanged)
#     first_contact:   19603 chars, 19 bullets reach this mode  (unchanged)
#     people_trigger:   4690 chars,  3 bullets reach this mode  (+256)
#     event_trigger:    4033 chars,  2 bullets reach this mode  (+76)
#
# known_sender/first_contact stayed close to the old flat-prompt size because
# most bullets reason about tools registered in both interactive modes; the
# real reduction is in the two proactive-trigger modes, which now only carry
# the guidance their bound capability can act on. Both proactive modes grew
# slightly when the misdirected guidance was rewritten rather than deleted -
# the cost of keeping the commitment - and both stay far inside their bound.
_MAX_INTERACTIVE_PROMPT_CHARS = 21000
_MAX_PROACTIVE_PROMPT_CHARS = 5000
_MAX_BULLET_CHARS = 2500


def test_interactive_prompts_stay_within_their_recorded_size_bounds() -> None:
    for mode in (FIRST_CONTACT, KNOWN_SENDER):
        assert len(SYSTEM_PROMPTS[mode]) <= _MAX_INTERACTIVE_PROMPT_CHARS, mode


def test_proactive_prompts_stay_within_their_recorded_size_bounds() -> None:
    for mode in (PEOPLE_TRIGGER, EVENT_TRIGGER):
        assert len(SYSTEM_PROMPTS[mode]) <= _MAX_PROACTIVE_PROMPT_CHARS, mode


def test_no_single_judgment_bullet_becomes_a_wall_of_text() -> None:
    """Bullet count is a poor proxy; per-bullet length is the checkable one.

    Merging bullets can lower the count while making the guidance harder to
    follow, so this bounds the outlier rather than the total.
    """
    assert JUDGMENT_BULLETS, "no judgment bullets defined"
    longest = max(JUDGMENT_BULLETS, key=lambda bullet: len(bullet.text))
    assert len(longest.text) <= _MAX_BULLET_CHARS, longest.text[:120]
