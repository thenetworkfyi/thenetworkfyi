import re

from thenetwork.agent.prompts import SYSTEM_PROMPT


def test_attachment_guidance_requires_an_accurate_sender_notice() -> None:
    guidance = SYSTEM_PROMPT.split("- Attachments:", 1)[1].split(
        "- `search` similarity", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "`Attachments present but not read: N`" in guidance
    assert "attachment was not read" in guidance
    assert "via `reply_to_sender`" in guidance
    assert "paste any relevant content into the email" in guidance
    assert "When the line is absent, do not mention attachments" in guidance
    for inaccurate in ("removed", "stripped", "deleted"):
        assert inaccurate not in guidance.lower()


def test_link_guidance_does_not_imply_fetch_capability() -> None:
    guidance = SYSTEM_PROMPT.split("- Links:", 1)[1].split("- `search` similarity", 1)[
        0
    ]
    guidance = " ".join(guidance.split())

    assert "cannot open links or read what is behind them" in guidance
    assert "visible reference, not as page content" in guidance
    assert "do not infer the destination's contents" in guidance
    assert "ask the sender what is behind the link" in guidance
    assert "trailing `…`" in guidance
    assert "incomplete reference" in guidance


def test_agent_email_tools_require_content_free_sent_summary() -> None:
    assert "Every `reply_to_sender` or `send_outreach` call" in SYSTEM_PROMPT
    assert "`sent_email_summary`" in SYSTEM_PROMPT
    assert "Do not copy the subject or body" in SYSTEM_PROMPT
    assert "include an address or headers" in SYSTEM_PROMPT


def test_possible_match_guidance_describes_current_email_capability() -> None:
    intro_guidance = SYSTEM_PROMPT.split("- A possible match:", 1)[1].split(
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
    guidance = SYSTEM_PROMPT.split("- `search` similarity", 1)[1].split(
        "- A `search` candidate", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "candidate discovery, not a fit score" in guidance
    assert "specific two-sided match thesis" in guidance
    assert "what each person is seeking" in guidance
    assert "what the other could materially contribute" in guidance
    assert "shared keyword, tool, title, topic, or city" in guidance
    assert "Missing or contradictory evidence" in guidance
    assert "act without interrogating" in guidance


def test_sender_owned_group_is_the_only_search_evidence_with_memory_ids() -> None:
    guidance = SYSTEM_PROMPT.split("- A `search` candidate", 1)[1].split(
        "- Tool status vocabulary", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "groups the sender's own evidence" in guidance
    assert "Only its evidence items carry `memory_id`" in guidance
    assert "Cross-user evidence items contain sealed `gist` only" in guidance
    assert "Never pass the sender-owned candidate's `person_id`" in guidance


def test_thin_standing_intent_guidance_requests_one_material_follow_up() -> None:
    guidance = SYSTEM_PROMPT.split("- Asking for clarification:", 1)[1].split(
        "- Progressive qualification memory:", 1
    )[0]
    guidance = " ".join(guidance.split())

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
    # The passive-promise prohibition moved from a scattered negative aside
    # ("passively promising", "requires the question") into one checkable
    # before-you-send constraint below; the commitment is unchanged.
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
    guidance = SYSTEM_PROMPT.split("- Asking for clarification:", 1)[1].split(
        "- Progressive qualification memory:", 1
    )[0]
    guidance = " ".join(guidance.split())

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
    guidance = SYSTEM_PROMPT.split("- Not every message is a career request.", 1)[
        1
    ].split("- Progressive qualification memory:", 1)[0]
    guidance = " ".join(guidance.split())

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
    guidance = SYSTEM_PROMPT.split("- Breadth is one fact, not many.", 1)[1].split(
        "- `register_person` is", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "their ask is broad" in guidance
    assert "a single standing note" in guidance
    assert "rather than a separate durable fact per named field" in guidance
    assert "will not catch them" in guidance
    assert "is not evidence of an interest" in guidance
    assert "instead of banking each new label" in guidance


def test_match_thesis_constraints_are_not_only_professional() -> None:
    guidance = SYSTEM_PROMPT.split("- `search` similarity is", 1)[1].split(
        "- A `search` candidate", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "Consequential constraints are not only professional" in guidance
    assert "level, scene, locality, format" in guidance


def test_progressive_qualification_replaces_one_enriched_intent_note() -> None:
    guidance = SYSTEM_PROMPT.split("- Progressive qualification memory:", 1)[1].split(
        "- Preferences about who", 1
    )[0]
    guidance = " ".join(guidance.split())

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


def test_under_supported_proactive_people_candidate_is_no_action() -> None:
    guidance = SYSTEM_PROMPT.split("- Proactive people triggers", 1)[1].split(
        "- Events are secondary", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "surface candidates; they do not establish fit" in guidance
    assert "same two-sided thesis" in guidance
    assert "call `no_action`" in guidance
    assert "Do not call `propose_introduction`" in guidance
    assert "under-supported proactive candidate" in guidance


def test_status_vocabulary_guidance_present() -> None:
    guidance = SYSTEM_PROMPT.split("- Tool status vocabulary:", 1)[1].split(
        "- `forget` deletion", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "tools never crash" in guidance
    assert "`limited` or `deferred`" in guidance
    assert "do not retry the same tool call" in guidance
    assert "`forbidden` means the action is structurally disallowed" in guidance
    assert "never work around it" in guidance
    assert "`error` with a `reason` means fix the input once" in guidance
    assert "never loop on the same error" in guidance


def test_operational_and_account_wide_requests_escalate() -> None:
    guidance = SYSTEM_PROMPT.split("- Operational and account-wide requests", 1)[
        1
    ].split("- Consolidation:", 1)[0]
    guidance = " ".join(guidance.split())

    assert "broken or not working as expected" in guidance
    assert "complaint that needs follow-up" in guidance
    assert "delete their account or all data" in guidance
    assert "call `escalate(reason)`" in guidance
    assert "Do not attempt remediation" in guidance
    assert "call `forget`" in guidance
    assert "request to forget or correct one or more specific" in guidance
    assert "should use `forget`" in guidance


def test_consolidation_guidance_present() -> None:
    guidance = SYSTEM_PROMPT.split("- Consolidation:", 1)[1].split(
        "- `register_person`", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "`consolidation_candidates`" in guidance
    assert "stale version of the fact you just saved" in guidance
    assert "`forget` the stale one (edit = forget + remember" in guidance
    assert "Do not forget a candidate that is merely related" in guidance
    assert "Only a memory solely about the sender can be forgotten" in guidance
    assert "co-owned (multi-ref) memory is protected" in guidance
    assert "`status=forbidden`" in guidance


def test_register_person_exists_guidance_present() -> None:
    guidance = SYSTEM_PROMPT.split("- `register_person`", 1)[1].split(
        "- A `search` result's `person_id`", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "`status=exists`" in guidance
    assert "use the returned id and continue normally" in guidance
    assert "this is not a failure" in guidance
    assert "does not need" in guidance and "retry" in guidance


def test_unknown_sender_can_be_answered_without_registration_guidance() -> None:
    guidance = SYSTEM_PROMPT.split("- `register_person`", 1)[1].split(
        "- Asking for clarification:", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "only asking what the service does" in guidance
    assert "explicitly declines participation or data retention" in guidance
    assert "without saving a memory or escalating" in guidance
    assert "authenticated inbound address without registering" in guidance


def test_first_contact_is_model_directed_and_uses_plain_public_language() -> None:
    guidance = SYSTEM_PROMPT.split("- First contact is a judgment call", 1)[1].split(
        "- A `search` result's `person_id`", 1
    )[0]
    guidance = " ".join(guidance.split())

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


def _event_guidance() -> str:
    guidance = SYSTEM_PROMPT.split("- Events are secondary:", 1)[1].split(
        "- First contact", 1
    )[0]
    return " ".join(guidance.split())


def test_event_guidance_keeps_events_secondary_and_relevance_separate() -> None:
    guidance = _event_guidance()

    assert "core value is making unusually relevant people connections" in guidance
    assert "Judge event relevance separately" in guidance
    assert "strong people match does not make an event relevant" in guidance
    assert "relevant event is never a reason to call `propose_introduction`" in guidance


def test_event_submission_and_interest_guidance_preserves_freeform_meaning() -> None:
    guidance = _event_guidance()

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
    guidance = _event_guidance()

    assert "Compare those gists carefully" in guidance
    assert "High semantic similarity is not enough" in guidance
    assert "call `send_event_recommendation` with the trigger's event id" in guidance
    assert "composes the concise FYI server-side" in guidance
    assert (
        "never use `send_outreach`, `reply_to_sender`, or model-written copy"
        in guidance
    )
    assert "never call `propose_introduction` during an event trigger" in guidance


def test_event_permission_is_scoped_without_service_promises() -> None:
    guidance = _event_guidance()

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


# The production model is a 31B instruct model, so the binding constraint on
# this prompt is instruction adherence across a long system message, not
# context capacity - the window is far larger than anything here. There is no
# published size cliff to point at, so these bounds are drift alarms rather
# than targets, and the only correct response to a breach is consolidating
# overlapping guidance. Never buy headroom by deleting a behavioral
# commitment: every one of them is pinned by a test above precisely so that
# shortcut fails loudly.
#
# Measured the same way at every point of comparison (rendered SYSTEM_PROMPT,
# not the source file - `wc -c` on prompts.py counts the backslash
# line-continuation syntax, which inflates the figure by roughly 580
# characters and never reaches the model):
#
#   08f8114 (before the prompt-adjustments project): 22493 chars, 20 bullets,
#     median bullet 725, mean 816, longest 1958
#   eff77d6 (round 1 assembled):                     22871 chars, 16 bullets,
#     median 797, mean 1054, longest 2669
#
# Round 1 grew the prompt by 378 characters, which is correct: it added two
# genuinely new commitments (breadth-is-one-fact, and the passive-promise
# check on an unsupported connection request). Its bullet-count reduction,
# however, came from concatenation - four event bullets became one - which
# raised the mean bullet length by 29% and is the worse shape for adherence
# even at equal total length. Splitting those back apart is what this bound
# protects.
_MAX_PROMPT_CHARS = 24000
_MAX_BULLET_CHARS = 2500


def _judgment_bullets() -> list[str]:
    """The judgment-notes bullets, as the model receives them."""
    block = SYSTEM_PROMPT.split(
        "Judgment notes that go beyond the tool descriptions:", 1
    )[1]
    block = block.split("\n\nUntrusted content:", 1)[0]
    return [("- " + part).strip() for part in re.split(r"^- ", block, flags=re.M)[1:]]


def test_system_prompt_stays_within_its_recorded_size_bounds() -> None:
    assert len(SYSTEM_PROMPT) <= _MAX_PROMPT_CHARS


def test_no_single_judgment_bullet_becomes_a_wall_of_text() -> None:
    """Bullet count is a poor proxy; per-bullet length is the checkable one.

    Merging bullets can lower the count while making the guidance harder to
    follow, so this bounds the outlier rather than the total.
    """
    bullets = _judgment_bullets()

    assert bullets, "judgment-notes block did not parse into bullets"
    longest = max(bullets, key=len)
    assert len(longest) <= _MAX_BULLET_CHARS, longest[:120]
