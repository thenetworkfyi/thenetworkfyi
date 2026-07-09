from thenetwork.agent.prompts import SYSTEM_PROMPT


def test_possible_match_guidance_describes_current_email_capability() -> None:
    intro_guidance = SYSTEM_PROMPT.split("- A possible match:", 1)[1].split(
        "- A one-way share", 1
    )[0]
    intro_guidance = " ".join(intro_guidance.split())

    assert "each party separately" in intro_guidance
    assert "anonymized note" in intro_guidance
    assert "no names or contact details are shared" in intro_guidance
    assert "Do not promise a separate connecting or follow-up email" in intro_guidance
    assert "do not imply that the parties can contact each other directly" in intro_guidance
