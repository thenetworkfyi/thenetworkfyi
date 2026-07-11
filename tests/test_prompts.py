from thenetwork.agent.prompts import SYSTEM_PROMPT


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
