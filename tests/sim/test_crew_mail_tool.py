"""Unit tests for CrewAI mailbox tool and MIME email formatting."""

from email.message import EmailMessage

from thenetwork.sim.personas.crew_mail_tool import (
    SimMailboxTool,
    build_sim_email_message,
)
from thenetwork.sim.personas.persona import (
    EmailFormat,
    EmailPresentation,
    EmailSignature,
    PersonaConfig,
    SignatureLink,
)
from thenetwork.sim.run.mail import SimPostOffice, _extract_body


def _config(**overrides) -> PersonaConfig:
    defaults = dict(
        name="Priya Shah",
        email="priya@example.test",
        goal="Find ML infrastructure operators.",
        stop_condition="Stop once introduced.",
        message_budget=2,
        agent_address="join@example.test",
    )
    defaults.update(overrides)
    return PersonaConfig(**defaults)


def test_build_sim_email_message_plain_text():
    config = _config()
    msg = build_sim_email_message(
        config, "Hi, I run ML platforms.", tick=1, subject="Sim Start"
    )

    assert msg["From"] == "Priya Shah <priya@example.test>"
    assert msg["To"] == "join@example.test"
    assert msg["Subject"] == "Sim Start"
    assert msg["X-Sim-Tick"] == "1"
    assert msg["X-Sim-Direction"] == "persona->agent"
    assert msg["X-Sim-Persona"] == "Priya Shah"
    assert "Message-ID" in msg
    assert _extract_body(msg).strip() == "Hi, I run ML platforms."


def test_build_sim_email_message_reply_threading_and_signature():
    config = _config(
        presentation=EmailPresentation(
            format=EmailFormat.MULTIPART_ALTERNATIVE,
            signature=EmailSignature(
                lines=("Priya Shah", "Lead"),
                link=SignatureLink(
                    text="Company", url="https://example.test/priya"
                ),
            ),
        )
    )
    reply_to = EmailMessage()
    reply_to["Subject"] = "Intro Request [intro:123]"
    reply_to["Message-ID"] = "<request@example.test>"
    reply_to["Reply-To"] = "agent@example.test"
    reply_to.set_content("Do you accept the introduction?")

    msg = build_sim_email_message(
        config, "YES\n[intro:123]", tick=2, reply_to=reply_to
    )

    assert msg["To"] == "agent@example.test"
    assert msg["Subject"] == "Re: Intro Request [intro:123]"
    assert msg["In-Reply-To"] == "<request@example.test>"
    assert msg["References"] == "<request@example.test>"
    assert msg.get_content_type() == "multipart/alternative"

    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "YES\n[intro:123]" in plain
    assert "-- \nPriya Shah\nLead\nCompany" in plain
    assert "> Do you accept the introduction?" in plain
    assert '<a href="https://example.test/priya">' in html


def test_sim_mailbox_tool_read_and_send():
    config = _config()
    post_office = SimPostOffice()

    # Deliver an incoming message from agent to persona
    incoming = EmailMessage()
    incoming["From"] = "join@example.test"
    incoming["To"] = "priya@example.test"
    incoming["Subject"] = "Welcome"
    incoming.set_content("Hello Priya!")
    post_office.deliver(incoming)

    tool = SimMailboxTool(config=config, post_office=post_office, tick=1)

    # Test reading
    read_output = tool._run(action="read")
    assert "From: join@example.test" in read_output
    assert "Hello Priya!" in read_output
    assert tool._run(action="read") == "No unread messages."
    assert post_office.pop_all(config.email) == ()

    # Test sending first email
    send_output = tool._run(action="send", body="Hello, excited to join!", subject="Hello")
    assert send_output == "Email sent successfully."
    assert tool.messages_sent == 1

    # Verify message delivered to post office
    delivered = post_office.messages_for("join@example.test")
    assert len(delivered) == 1
    assert _extract_body(delivered[0]).strip() == "Hello, excited to join!"

    # Test sending second email (reaching budget cap = 2)
    tool._run(action="send", body="Second message", subject="Re: Hello")
    assert tool.messages_sent == 2

    # Test sending third email (budget exhausted)
    exhausted_output = tool._run(action="send", body="Third message")
    assert "budget exhausted" in exhausted_output.lower()


def test_sim_mailbox_tool_normalizes_consent_reply_for_active_thread():
    config = _config()
    post_office = SimPostOffice()
    active_token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    other_token = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    reply_to = EmailMessage()
    reply_to["Subject"] = f"Intro Request [intro:{active_token}]"
    reply_to["Message-ID"] = "<request@example.test>"
    reply_to.set_content("Do you accept the introduction?")
    tool = SimMailboxTool(config=config, post_office=post_office)
    tool.update_turn(tick=4, reply_to=reply_to)

    assert (
        tool._run(
            action="send", body=f"YES\n[intro:{other_token}]", subject="Reply"
        )
        == "Email sent successfully."
    )

    delivered = post_office.messages_for(config.agent_address)
    assert len(delivered) == 1
    body = _extract_body(delivered[0])
    assert f"YES\n[intro:{active_token}]" in body
    assert f"[intro:{other_token}]" not in body
    assert delivered[0]["X-Sim-Tick"] == "4"
    assert delivered[0]["In-Reply-To"] == "<request@example.test>"


def test_sim_mailbox_tool_budget_is_shared_across_tool_instances():
    config = _config(message_budget=1)
    post_office = SimPostOffice()

    first_turn = SimMailboxTool(config=config, post_office=post_office, tick=1)
    assert first_turn._run(action="send", body="First") == "Email sent successfully."

    second_turn = SimMailboxTool(config=config, post_office=post_office, tick=2)
    result = second_turn._run(action="send", body="Second")

    assert result == "Error: Message budget exhausted."
    assert second_turn.messages_sent == 1
    assert len(post_office.messages_for(config.agent_address)) == 1
