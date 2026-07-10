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
    assert "Do not interrogate every message" in guidance
    assert "consent reply or a concrete update" in guidance
