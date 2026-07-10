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
