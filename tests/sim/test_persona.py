from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock

import pytest

from thenetwork.sim.personas.persona import (
    EmailFormat,
    EmailPresentation,
    EmailSignature,
    PersonaConfig,
    SignatureLink,
    TinyPersonEmailAdapter,
)
from thenetwork.sim.html_validation import html_to_visible_text
from thenetwork.sim.scenarios import StrongMatchScenario, default_strong_match_configs


class ScriptedTinyPerson:
    def __init__(self, name: str, replies: list[str]) -> None:
        self.name = name
        self.replies = replies
        self.stimuli: list[str] = []

    def listen_and_act(self, stimulus: str):
        self.stimuli.append(stimulus)
        return {"action": {"content": self.replies.pop(0)}}


def test_tinyperson_adapter_returns_email_message_and_tracks_budget():
    person = ScriptedTinyPerson("Priya", ["I want to meet ML infra operators."])
    config = PersonaConfig(
        name="Priya Shah",
        email="priya@example.test",
        goal="Find ML infrastructure operators.",
        stop_condition="Stop after one message.",
        message_budget=1,
        agent_address="join@example.test",
    )
    adapter = TinyPersonEmailAdapter(person, config)

    msg = adapter.next_email("write", tick=7, subject="Hello")

    assert msg is not None
    assert msg["From"] == "Priya Shah <priya@example.test>"
    assert msg["To"] == "join@example.test"
    assert msg["Subject"] == "Hello"
    assert msg["X-Sim-Tick"] == "7"
    assert msg["X-Sim-Direction"] == "persona->agent"
    assert msg["X-Sim-Persona"] == "Priya Shah"
    assert "ML infra operators" in msg.get_content()
    assert adapter.next_email("again", tick=8) is None


def test_tinyperson_adapter_builds_threaded_reply_with_quoted_original():
    person = ScriptedTinyPerson("Priya", ["YES"])
    adapter = TinyPersonEmailAdapter(
        person,
        PersonaConfig(
            name="Priya Shah",
            email="priya@example.test",
            goal="Find ML infrastructure operators.",
            stop_condition="Stop after one message.",
            agent_address="join@example.test",
        ),
    )
    request = EmailMessage()
    request["Subject"] = (
        "Possible introduction [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"
    )
    request["Message-ID"] = "<proposal@example.test>"
    request["References"] = "<opening@example.test>"
    request.set_content("A possible match came up.\n\nReply YES to opt in.")

    msg = adapter.next_email("write", tick=2, reply_to=request)

    assert msg is not None
    assert msg["Subject"] == (
        "Re: Possible introduction [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"
    )
    assert msg["In-Reply-To"] == "<proposal@example.test>"
    assert msg["References"] == "<opening@example.test> <proposal@example.test>"
    assert msg.get_content() == (
        "YES\n\n> A possible match came up.\n>\n> Reply YES to opt in.\n"
    )


def test_tinyperson_adapter_renders_plain_signature():
    person = ScriptedTinyPerson("Priya", ["I want to meet ML infra operators."])
    adapter = TinyPersonEmailAdapter(
        person,
        PersonaConfig(
            name="Priya Shah",
            email="priya@example.test",
            goal="Find ML infrastructure operators.",
            stop_condition="Stop after one message.",
            presentation=EmailPresentation(
                signature=EmailSignature(
                    lines=("Priya Shah", "ML Platform Lead", "Northstar Labs")
                )
            ),
        ),
    )

    msg = adapter.next_email("write", tick=1)

    assert msg is not None
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content() == (
        "I want to meet ML infra operators.\n\n"
        "-- \nPriya Shah\nML Platform Lead\nNorthstar Labs\n"
    )


def test_tinyperson_adapter_renders_escaped_multipart_linked_signature():
    authored = "I work on <factory> ML & reliability.\nLet us compare notes."
    person = ScriptedTinyPerson("Priya", [authored])
    adapter = TinyPersonEmailAdapter(
        person,
        PersonaConfig(
            name="Priya Shah",
            email="priya@example.test",
            goal="Find ML infrastructure operators.",
            stop_condition="Stop after one message.",
            presentation=EmailPresentation(
                format=EmailFormat.MULTIPART_ALTERNATIVE,
                signature=EmailSignature(
                    lines=("Priya <Shah>", "ML & Platform Lead"),
                    link=SignatureLink(
                        text="Northstar R&D",
                        url="https://northstar.example/about?team=ml&source=sim",
                    ),
                ),
            ),
        ),
    )

    msg = adapter.next_email("write", tick=1)

    assert msg is not None
    assert msg.get_content_type() == "multipart/alternative"
    assert tuple(part.get_content_type() for part in msg.iter_parts()) == (
        "text/plain",
        "text/html",
    )
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert authored in plain
    assert "Priya <Shah>" in plain
    assert "Northstar R&D" in plain
    assert html_to_visible_text(html) == " ".join(plain.split())
    assert "I work on &lt;factory&gt; ML &amp; reliability." in html
    assert "Priya &lt;Shah&gt;" in html
    assert 'href="https://northstar.example/about?team=ml&amp;source=sim"' in html
    assert ">Northstar R&amp;D</a>" in html


def test_tinyperson_adapter_renders_equivalent_multipart_reply_and_signature():
    person = ScriptedTinyPerson("Priya", ["YES & thanks"])
    adapter = TinyPersonEmailAdapter(
        person,
        PersonaConfig(
            name="Priya Shah",
            email="priya@example.test",
            goal="Find ML infrastructure operators.",
            stop_condition="Stop after one message.",
            presentation=EmailPresentation(
                format=EmailFormat.MULTIPART_ALTERNATIVE,
                signature=EmailSignature(lines=("Priya Shah",)),
            ),
        ),
    )
    request = EmailMessage()
    request["Subject"] = "Possible introduction"
    request["Message-ID"] = "<proposal@example.test>"
    request.set_content("A possible <match> came up.\n\nReply YES to opt in.")

    msg = adapter.next_email("write", tick=2, reply_to=request)

    assert msg is not None
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert plain == (
        "YES & thanks\n\n-- \nPriya Shah\n\n"
        "> A possible <match> came up.\n>\n> Reply YES to opt in.\n"
    )
    assert "<blockquote>" in html
    assert "A possible &lt;match&gt; came up." in html
    assert html_to_visible_text(html) == (
        "YES & thanks -- Priya Shah A possible <match> came up. Reply YES to opt in."
    )
    assert msg["In-Reply-To"] == "<proposal@example.test>"
    assert adapter.messages_sent == 1


@pytest.mark.parametrize(
    "url",
    ("javascript:alert(1)", "//example.test/profile", "mailto:priya@example.test"),
)
def test_signature_link_rejects_non_http_urls(url):
    with pytest.raises(ValueError, match="absolute http"):
        SignatureLink(text="Profile", url=url)


def test_tinyperson_adapter_replies_to_proxy_reply_to_address():
    person = ScriptedTinyPerson("Priya", ["Thanks, I would like to compare notes."])
    adapter = TinyPersonEmailAdapter(
        person,
        PersonaConfig(
            name="Priya Shah",
            email="priya@example.test",
            goal="Find ML infrastructure operators.",
            stop_condition="Stop after one exchange.",
            agent_address="join@example.test",
        ),
    )
    introduction = EmailMessage()
    introduction["From"] = (
        "The Network <hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test>"
    )
    introduction["Reply-To"] = (
        "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test"
    )
    introduction["To"] = "priya@example.test"
    introduction["Subject"] = "Your introduction"
    introduction.set_content("Priya and Samir, you both opted in.")

    reply = adapter.next_email("write", tick=4, reply_to=introduction)

    assert reply is not None
    assert reply["To"] == (
        "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test"
    )


@pytest.mark.asyncio
async def test_strong_match_scenario_replays_two_personas_to_process_email(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = (
        TinyPersonEmailAdapter(
            ScriptedTinyPerson("Priya", ["I need ML infra help in factories."]),
            configs[0],
        ),
        TinyPersonEmailAdapter(
            ScriptedTinyPerson("Samir", ["I deploy ML infra for factories."]),
            configs[1],
        ),
    )
    process = AsyncMock()

    result = await StrongMatchScenario(adapters, run_dir=tmp_path).run(process=process)

    assert result.persona_message_count == 2
    assert process.await_count == 2
    assert result.mbox_path.exists()
    assert result.transcript_path.exists()
    assert "Priya Shah" not in result.transcript
    assert "Samir Vale" not in result.transcript
    assert "I need ML infra help in factories." not in result.transcript
    assert "I deploy ML infra for factories." not in result.transcript
    raw_messages = result.post_office.messages_for("join@example.test")
    assert raw_messages[0].get_content().strip() == "I need ML infra help in factories."
    assert raw_messages[1].get_content().strip() == "I deploy ML infra for factories."
