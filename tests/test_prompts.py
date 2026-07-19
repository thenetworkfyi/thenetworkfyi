from thenetwork.agent.prompts import SYSTEM_PROMPT


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


def test_vague_standing_intent_guidance_requests_a_follow_up() -> None:
    guidance = SYSTEM_PROMPT.split("- Asking for clarification:", 1)[1].split(
        "- First contact", 1
    )[0]
    guidance = " ".join(guidance.split())

    assert "new or existing member" in guidance
    assert "one brief, concrete, curious question" in guidance
    assert "sector, stage, or connection" in guidance
    assert "This is a qualification turn" in guidance
    assert "do not call `propose_introduction`" in guidance
    assert "even if `search` found a semantically adjacent person" in guidance
    assert "Do not interrogate every message" in guidance
    assert "consent reply or a concrete update" in guidance
    assert "`remember` the specific interest learned under the sender's id" in guidance


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
