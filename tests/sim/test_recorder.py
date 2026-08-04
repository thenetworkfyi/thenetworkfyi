from __future__ import annotations

import hashlib
import json
import mailbox
import re
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from procrastinate import utils as procrastinate_utils
from procrastinate.testing import InMemoryConnector
from pydantic_ai.exceptions import ModelHTTPError

from thenetwork.audit import audit_event, audit_model_trace
from thenetwork.agent.prompts import (
    EVENT_TRIGGER,
    FIRST_CONTACT,
    KNOWN_SENDER,
    PEOPLE_TRIGGER,
    SYSTEM_PROMPTS,
)
from thenetwork.db.models import (
    Event,
    EventRecommendation,
    IntroductionConsent,
    Memory,
    Person,
)
from thenetwork.email.outbound import send_proxy_introduction, send_relay_email
from thenetwork.email.render import standard_signature_lines
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.sim.cli import main, run_sim
from thenetwork.sim.run.mail import SimPostOffice, publish_redacted_mbox
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter
from thenetwork.sim.personas.llm_persona import _PERSONA_PROMPT
from thenetwork.sim.personas.population import DEFAULT_OUTCOME_CHECKS
from thenetwork.sim.run.recorder import (
    EventsLog,
    SimRunArtifacts,
    SimRunConfig,
    SimRunRecorder,
    _SimulationJobDrainer,
    _assemble_scenario_outcome,
    _config_payload,
    _database_outcome_state,
    _event_correlation_key,
    _mail_facts,
    _recording_process,
    write_redacted_json,
)
from thenetwork.sim.scenarios import default_strong_match_configs
from thenetwork.sim.scoring.scoring import (
    EventOutcomeFact,
    EventRecommendationOutcomeFact,
    IntroductionConsentState,
    MemoryExpectation,
    OutcomeCheck,
    ProactiveEventTriggerOutcomeFact,
)
from thenetwork.worker.tasks import app, process_email


class ScriptedTinyPerson:
    def __init__(self, body: str) -> None:
        self.name = "Scripted"
        self.body = body

    def listen_and_act(self, _stimulus: str):
        return {"content": self.body}


# Deterministic stand-in for the local span classifier the redactor runs on.
#
# The real weights are a multi-gigabyte download and CI runs
# `pytest -m "not integration"`, so no test in this module may load them. These
# patterns produce spans shaped exactly like the pipeline's output, with the
# labels the real model assigned to these same strings; the redactor's own
# behavior against real weights is pinned by tests/test_log_redaction.py's
# `real_sanitizer` tier.
_FAKE_SPAN_PATTERNS = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "private_email"),
    (re.compile(r"https?://\S+"), "private_url"),
    (re.compile(r"\b\d{3}-\d{3}-\d{4}\b"), "private_phone"),
    (
        re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b"),
        "account_number",
    ),
    # The real model recognizes given names generically; a stub cannot, so the
    # persona names these tests assert on are listed explicitly.
    (
        re.compile(
            r"\b(?:Alice(?: Example| Shah)?|Bob(?: Lee)?|Petra|Private Persona Name)\b"
        ),
        "private_person",
    ),
)


def fake_classify(text: str) -> list[dict]:
    spans = [
        {
            "entity_group": label,
            "start": match.start(),
            "end": match.end(),
            "score": 0.99,
        }
        for pattern, label in _FAKE_SPAN_PATTERNS
        for match in pattern.finditer(text)
    ]
    spans.sort(key=lambda span: (span["start"], span["end"]))
    return spans


@pytest.fixture(autouse=True)
def _stub_span_classifier(monkeypatch):
    """Keep every test in this module off the real sanitizer weights."""
    from thenetwork.memory import sanitize as sanitize_mod

    monkeypatch.setattr(sanitize_mod, "_get_privacy_filter", lambda: fake_classify)


@pytest.mark.asyncio
async def test_run_recorder_writes_config_mbox_transcript_and_events(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = tuple(
        TinyPersonEmailAdapter(ScriptedTinyPerson(f"body {index}"), config)
        for index, config in enumerate(configs, start=1)
    )
    recorder = SimRunRecorder(
        runs_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 8, 1, 2, 3, tzinfo=timezone.utc),
    )

    artifacts = await recorder.run(
        adapters,
        SimRunConfig(
            scenario="strong-match",
            ticks=1,
            proactive_every=10,
            personas=configs,
        ),
    )

    assert artifacts.run_dir == tmp_path / "20260708T010203Z"
    assert artifacts.config_path.exists()
    assert artifacts.mbox_path.exists()
    assert artifacts.transcript_path.exists()
    assert artifacts.events_path.exists()
    config = json.loads(artifacts.config_path.read_text())
    assert config["scenario"] == "strong-match"
    assert len(config["personas"]) == 2
    assert "body 1" not in artifacts.transcript_path.read_text()
    assert "body 1" in artifacts.raw_mbox_path.read_text()
    assert artifacts.private_dir.stat().st_mode & 0o777 == 0o700
    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    assert events[0]["event"] == "sim.run_started"
    assert any(event["event"] == "sim.tick_completed" for event in events)
    assert events[-1]["event"] == "sim.run_completed"


@pytest.mark.asyncio
async def test_public_simulation_artifacts_redact_content_and_keep_raw_mail_private(
    tmp_path, monkeypatch
):
    persona = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Alice uses https://example.test/path and 415-555-0199",
        stop_condition="Wait for a match.",
        agent_address="join@example.test",
    )
    artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
        (TinyPersonEmailAdapter(ScriptedTinyPerson(persona.goal), persona),),
        SimRunConfig(
            scenario="redaction",
            ticks=1,
            proactive_every=None,
            personas=(persona,),
        ),
    )

    public_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            artifacts.config_path,
            artifacts.events_path,
            artifacts.mbox_path,
            artifacts.transcript_path,
        )
    )
    for sensitive in (
        "Alice",
        "alice@example.test",
        "https://example.test/path",
        "415-555-0199",
    ):
        assert sensitive not in public_artifacts
        assert sensitive in artifacts.raw_mbox_path.read_text(encoding="utf-8")
    assert artifacts.private_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_recorder_marks_absent_memory_fact_unexercised_with_bounded_evidence(
    tmp_path,
):
    persona = PersonaConfig(
        name="Petra",
        email="petra.sim@example.test",
        goal="Explore archival science.",
        stop_condition="Wait for a useful connection.",
        agent_address="join@example.test",
    )
    body = "I am interested in archival science and data management."
    artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
        (TinyPersonEmailAdapter(ScriptedTinyPerson(body), persona),),
        SimRunConfig(
            scenario="memory-exercise",
            ticks=1,
            proactive_every=None,
            personas=(persona,),
            expectations=(
                MemoryExpectation(
                    description="Petra provenance interest remembered",
                    gist_contains="provenance",
                    persona_email=persona.email,
                    inbound_contains_any=("provenance",),
                ),
            ),
        ),
    )

    tier2 = next(
        json.loads(line)
        for line in artifacts.events_path.read_text().splitlines()
        if json.loads(line)["event"] == "sim.score.tier2"
    )
    assert tier2["passed"] is True
    assert tier2["findings"][0]["evidence"] == {
        "unexercised": True,
        "persona_inbound_messages_checked": 1,
    }
    assert "unexercised" in tier2["findings"][0]["message"]

    public_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            artifacts.config_path,
            artifacts.events_path,
            artifacts.mbox_path,
            artifacts.transcript_path,
        )
    )
    assert body not in public_artifacts
    assert persona.email not in public_artifacts
    assert body in artifacts.raw_mbox_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_recorder_combines_anonymous_tier1_and_persona_bound_tier2(tmp_path):
    alice = PersonaConfig(
        name="Alice Shah",
        email="alice.sim@example.test",
        goal="Record my provenance work.",
        stop_condition="Wait for a useful connection.",
        agent_address="join@example.test",
    )
    bob = PersonaConfig(
        name="Bob Lee",
        email="bob.sim@example.test",
        goal="Say hello without discussing bakery work.",
        stop_condition="Wait for a useful connection.",
        agent_address="join@example.test",
    )
    private_fact = "I work on museum provenance systems."
    memories = (
        Memory(
            id="memory-alice",
            text="raw",
            refs=[alice.email],
            gist="works on museum provenance systems",
        ),
    )

    async def process(**kwargs):
        if kwargs["sender_email"] != alice.email:
            return
        send_proxy_introduction(
            person_a_email=alice.email,
            person_b_email=bob.email,
            person_a_gist="Works on museum provenance systems",
            person_b_gist="Explores archival data management",
            reply_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    outbound_settings = SimpleNamespace(
        relay_domain="relay.example.test",
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_account="join@example.test",
        smtp_password="secret",
    )
    with patch(
        "thenetwork.email.outbound.get_settings", return_value=outbound_settings
    ):
        artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
            (
                TinyPersonEmailAdapter(ScriptedTinyPerson(private_fact), alice),
                TinyPersonEmailAdapter(ScriptedTinyPerson("Hello."), bob),
            ),
            SimRunConfig(
                scenario="assembled-scoring",
                ticks=1,
                proactive_every=None,
                personas=(alice, bob),
                expectations=(
                    MemoryExpectation(
                        description="Alice provenance work is remembered",
                        gist_contains="provenance",
                        persona_email=alice.email,
                        inbound_contains_any=("provenance",),
                    ),
                    MemoryExpectation(
                        description="Bob bakery work is remembered",
                        gist_contains="bakery",
                        persona_email=bob.email,
                        inbound_contains_any=("bakery",),
                    ),
                ),
            ),
            process=process,
            memories=memories,
        )

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    tier1 = next(event for event in events if event["event"] == "sim.score.tier1")
    presentation = next(
        event for event in events if event["event"] == "sim.score.presentation"
    )
    tier2 = next(event for event in events if event["event"] == "sim.score.tier2")
    assert tier1["passed"] is True
    assert presentation == {
        "event": "sim.score.presentation",
        "findings": [
            {
                "evidence": {"messages_checked": 2},
                "message": "Captured user-facing MIME passed presentation checks",
                "passed": True,
                "tier": "presentation",
            }
        ],
        "passed": True,
    }
    assert tier2["passed"] is True
    assert tier2["findings"][0]["evidence"] == {"memory_id": "memory-alice"}
    assert tier2["findings"][1]["evidence"] == {
        "persona_inbound_messages_checked": 1,
        "unexercised": True,
    }

    raw_mail = artifacts.raw_mbox_path.read_text(encoding="utf-8")
    assert private_fact in raw_mail
    assert "Alice Shah and Bob Lee" not in raw_mail

    public_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            artifacts.config_path,
            artifacts.events_path,
            artifacts.mbox_path,
            artifacts.transcript_path,
        )
    )
    for private_value in (alice.name, alice.email, bob.name, bob.email, private_fact):
        assert private_value not in public_artifacts


@pytest.mark.asyncio
async def test_recorder_presentation_failure_has_bounded_stable_evidence(tmp_path):
    persona = PersonaConfig(
        name="Alice Shah",
        email="alice.sim@example.test",
        goal="Wait for a connection.",
        stop_condition="Wait.",
        agent_address="join@example.test",
    )
    token = "[intro:11111111-1111-1111-1111-111111111111]"
    private_body = (
        f"Malformed presentation {token}\n\n{'\n'.join(standard_signature_lines())}"
    )

    async def process(**_kwargs):
        send_relay_email(
            to_address=persona.email,
            proxy_address=(
                "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test"
            ),
            subject="Your introduction",
            body_text=private_body,
            automated=True,
        )

    artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
        (TinyPersonEmailAdapter(ScriptedTinyPerson("Hello."), persona),),
        SimRunConfig(
            scenario="presentation-failure",
            ticks=1,
            proactive_every=None,
            personas=(persona,),
        ),
        process=process,
    )

    presentation = next(
        json.loads(line)
        for line in artifacts.events_path.read_text().splitlines()
        if json.loads(line)["event"] == "sim.score.presentation"
    )
    assert presentation == {
        "event": "sim.score.presentation",
        "findings": [
            {
                "evidence": {
                    "message_index": 2,
                    "violations": [
                        "alternative_order",
                        "mime_type",
                        "required_text_html",
                        "required_text_plain",
                    ],
                },
                "message": "Captured user-facing MIME failed presentation checks",
                "passed": False,
                "tier": "presentation",
            }
        ],
        "passed": False,
    }
    public_artifacts = (
        artifacts.events_path.read_text() + artifacts.mbox_path.read_text()
    )
    assert private_body not in public_artifacts
    assert token not in public_artifacts
    assert persona.email not in public_artifacts


def test_public_simulation_mbox_redacts_untrusted_headers_and_envelope(tmp_path):
    raw_mbox_path = tmp_path / "private" / "all-mail.mbox"
    public_mbox_path = tmp_path / "all-mail.mbox"
    message = EmailMessage()
    message["From"] = "Alice <alice@example.test>"
    message["To"] = "join@example.test"
    source_message_id = (
        "<1772890.10662178711929224360@"
        "cbx-dl-71004e9f-install-content-scanner-a5839631>"
    )
    source_reply_id = "<trace_123456-alice@example.test>"
    source_content_id = "<attachment_123456-alice@example.test>"
    message["Message-ID"] = source_message_id
    message["In-Reply-To"] = source_reply_id
    message["References"] = f"{source_message_id} {source_reply_id}"
    message["X-Contact"] = "415-555-0199"
    message["X-Custom"] = "https://example.test/alice@example.test"
    message.set_content("Safe body")
    message.add_attachment(
        b"alice@example.test",
        maintype="application",
        subtype="octet-stream",
        filename="alice@example.test",
    )
    message.set_param("name", "alice@example.test", header="Content-Type")
    message["Content-ID"] = source_content_id
    SimPostOffice(mbox_path=raw_mbox_path).deliver(message)

    publish_redacted_mbox(raw_mbox_path, public_mbox_path)

    public_box = mailbox.mbox(public_mbox_path)
    try:
        (redacted,) = tuple(public_box)
    finally:
        public_box.close()
    assert redacted["Message-ID"] == "<sim-redacted-1@simulation.invalid>"
    assert redacted["In-Reply-To"] == "<sim-redacted-2@simulation.invalid>"
    assert redacted["References"] == (
        "<sim-redacted-1@simulation.invalid> <sim-redacted-2@simulation.invalid>"
    )
    assert redacted["Content-ID"] == "<sim-redacted-3@simulation.invalid>"

    public_artifact = public_mbox_path.read_text(encoding="utf-8")
    for sensitive in (
        "alice@example.test",
        "1772890.10662178711929224360",
        "cbx-dl-71004e9f-install-content-scanner-a5839631",
        "trace_123456",
        "attachment_123456",
        "415-555-0199",
        "https://example.test/alice@example.test",
        "Safe body",
    ):
        assert sensitive not in public_artifact
    assert "From MAILER-DAEMON" in public_artifact
    assert "X-Contact:" in public_artifact
    assert "[redacted-text chars=10]" in public_artifact


def test_public_simulation_mbox_consolidates_repeated_identifier_headers(tmp_path):
    raw_mbox_path = tmp_path / "private" / "all-mail.mbox"
    public_mbox_path = tmp_path / "all-mail.mbox"
    raw_message = (
        b"From: alice@example.test\r\n"
        b"To: join@example.test\r\n"
        b"Message-ID: <primary@private.example>\r\n"
        b"Message-ID: <duplicate@private.example>\r\n"
        b"In-Reply-To: <root@private.example>\r\n"
        b"In-Reply-To: <other@private.example>\r\n"
        b"References: <root@private.example>\r\n"
        b"References: <other@private.example>\r\n"
        b"Resent-Message-ID: <resent-primary@private.example>\r\n"
        b"Resent-Message-ID: <resent-duplicate@private.example>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Private body\r\n"
    )
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    assert len(message.get_all("Message-ID", ())) == 2
    SimPostOffice(mbox_path=raw_mbox_path).deliver(message)

    publish_redacted_mbox(raw_mbox_path, public_mbox_path)

    public_box = mailbox.mbox(public_mbox_path)
    try:
        (redacted,) = tuple(public_box)
    finally:
        public_box.close()
    assert redacted.get_all("Message-ID") == ["<sim-redacted-1@simulation.invalid>"]
    assert redacted.get_all("In-Reply-To") == [
        "<sim-redacted-2@simulation.invalid> <sim-redacted-3@simulation.invalid>"
    ]
    assert redacted.get_all("References") == [
        "<sim-redacted-2@simulation.invalid> <sim-redacted-3@simulation.invalid>"
    ]
    assert redacted.get_all("Resent-Message-ID") == [
        "<sim-redacted-4@simulation.invalid>"
    ]
    public_artifact = public_mbox_path.read_text(encoding="utf-8")
    assert "private.example" not in public_artifact
    assert "duplicate" not in public_artifact
    assert "Private body" not in public_artifact


def test_public_simulation_json_artifacts_omit_raw_markup_and_message_content(tmp_path):
    config_path = tmp_path / "config.json"
    events_path = tmp_path / "events.jsonl"
    markup = "<p>Untrusted message content</p>"

    write_redacted_json(config_path, {"body": markup})
    EventsLog(events_path).write("sim.fixture", subject=markup)

    public_artifact = config_path.read_text() + events_path.read_text()
    assert markup not in public_artifact
    assert "Untrusted message content" not in public_artifact
    assert public_artifact.count("[markup-omitted]") == 2


def test_public_simulation_mbox_redacts_both_alternatives_and_keeps_safe_order(
    tmp_path,
):
    raw_mbox_path = tmp_path / "private" / "all-mail.mbox"
    public_mbox_path = tmp_path / "all-mail.mbox"
    message = EmailMessage()
    message["From"] = "Alice <alice@example.test>"
    message["To"] = "join@example.test"
    message.set_content("Alice private plain text")
    message.add_alternative(
        "<p>Alice private <strong>HTML</strong> text</p>", subtype="html"
    )
    SimPostOffice(mbox_path=raw_mbox_path).deliver(message)

    publish_redacted_mbox(raw_mbox_path, public_mbox_path)

    public_box = mailbox.mbox(public_mbox_path)
    try:
        (redacted,) = tuple(public_box)
    finally:
        public_box.close()
    assert redacted.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in redacted.get_payload()] == [
        "text/plain",
        "text/html",
    ]
    public_artifact = public_mbox_path.read_text(encoding="utf-8")
    assert "Alice private plain text" not in public_artifact
    assert "Alice private" not in public_artifact
    assert "<strong>HTML</strong>" not in public_artifact
    assert public_artifact.count("[redacted-text chars=") == 2


@pytest.mark.asyncio
async def test_simulation_exception_text_is_redacted(tmp_path, monkeypatch):
    persona = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Find a collaborator.",
        stop_condition="Wait for a match.",
        agent_address="join@example.test",
    )

    async def failing_process(**_kwargs):
        raise RuntimeError("Alice exposed https://example.test/path and 415-555-0199")

    artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
        (TinyPersonEmailAdapter(ScriptedTinyPerson("Hello"), persona),),
        SimRunConfig(
            scenario="redaction-error",
            ticks=1,
            proactive_every=None,
            personas=(persona,),
        ),
        process=failing_process,
    )

    events = artifacts.events_path.read_text(encoding="utf-8")
    assert '"error_type": "RuntimeError"' in events
    assert "Alice" not in events
    assert "https://example.test/path" not in events
    assert "415-555-0199" not in events


@pytest.mark.asyncio
async def test_sim_run_cli_function_creates_run_directory(tmp_path):
    artifacts = await run_sim(runs_dir=tmp_path, ticks=1, proactive_every=10)

    assert artifacts.run_dir.parent == tmp_path
    assert artifacts.config_path.exists()
    assert artifacts.mbox_path.name == "all-mail.mbox"
    assert artifacts.transcript_path.name == "transcript.md"
    assert artifacts.events_path.name == "events.jsonl"
    assert artifacts.audit_path.name == "audit.jsonl"
    assert not artifacts.audit_path.exists()
    config = json.loads(artifacts.config_path.read_text())
    assert len(config["personas"]) == 29
    assert len(config["outcome_checks"]) == len(DEFAULT_OUTCOME_CHECKS)
    assert config["llm_personas"] is False
    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    assert {"sim.score.tier1", "sim.score.tier2", "sim.score.outcome"} <= {
        event["event"] for event in events
    }


@pytest.mark.asyncio
async def test_sim_run_persona_cap_remains_backward_compatible(tmp_path):
    artifacts = await run_sim(
        runs_dir=tmp_path,
        ticks=1,
        proactive_every=None,
        personas=10,
    )

    config = json.loads(artifacts.config_path.read_text())
    assert len(config["personas"]) == 10


@pytest.mark.asyncio
async def test_real_process_run_uses_and_records_per_run_database(tmp_path):
    expected = SimpleNamespace(
        run_dir=tmp_path / "run",
        raw_database_dump_path=tmp_path / "run" / "private" / "database.dump",
    )
    database_name = "sim_0123456789abcdef"

    with (
        patch(
            "thenetwork.sim.cli.new_sim_database_name",
            return_value=database_name,
        ),
        patch(
            "thenetwork.sim.cli.provision_sim_database",
            return_value=nullcontext(database_name),
        ) as provision,
        patch.object(
            SimRunRecorder,
            "run",
            AsyncMock(return_value=expected),
        ) as record,
    ):
        artifacts = await run_sim(
            runs_dir=tmp_path,
            ticks=1,
            proactive_every=None,
            mock_process=False,
            keep_db=True,
            personas=1,
        )

    assert artifacts is expected
    provision.assert_called_once()
    assert provision.call_args.args == (database_name,)
    assert provision.call_args.kwargs["keep"] is True
    assert provision.call_args.kwargs["dump_path"]() == expected.raw_database_dump_path
    recorded_config = record.await_args.args[1]
    assert recorded_config.mock_process is False
    assert recorded_config.database_name == database_name


@pytest.mark.asyncio
async def test_keep_db_is_rejected_for_mock_run(tmp_path):
    with pytest.raises(ValueError, match="keep_db requires mock_process=False"):
        await run_sim(
            runs_dir=tmp_path,
            ticks=1,
            proactive_every=None,
            keep_db=True,
        )


def test_sim_run_cli_streams_progress_to_stderr_and_only_path_to_stdout(
    tmp_path, capsys
):
    main(
        [
            "run",
            "--runs-dir",
            str(tmp_path),
            "--ticks",
            "2",
            "--personas",
            "1",
        ]
    )

    captured = capsys.readouterr()
    stdout_lines = captured.out.splitlines()
    assert len(stdout_lines) == 1
    assert Path(stdout_lines[0]).parent == tmp_path
    assert captured.err.splitlines() == [
        "tick 1/2: started",
        "tick 1/2: Priya Shah: process_email started",
        "tick 1/2: Priya Shah: process_email completed",
        "tick 1/2: completed (1 persona messages, 0 proactive jobs)",
        "tick 2/2: started",
        "tick 2/2: Priya Shah: process_email started",
        "tick 2/2: Priya Shah: process_email completed",
        "tick 2/2: completed (1 persona messages, 0 proactive jobs)",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_recorder_routes_through_real_process_email(tmp_path, seeded_db):
    config = PersonaConfig(
        name="Alice",
        email="alice@test.com",
        goal="Find a collaborator for a Rust project.",
        stop_condition="An introduction is made.",
        agent_address="join@thenetwork.test",
    )
    adapter = TinyPersonEmailAdapter(
        ScriptedTinyPerson(
            "I am Alice and I am looking for a Rust collaborator for my project."
        ),
        config,
    )
    recorder = SimRunRecorder(runs_dir=tmp_path)

    with (
        app.replace_connector(InMemoryConnector()),
        patch("thenetwork.worker.tasks.run_agent_for_email", AsyncMock()) as mock_agent,
    ):
        artifacts = await recorder.run(
            (adapter,),
            SimRunConfig(
                scenario="real-process",
                ticks=1,
                proactive_every=None,
                personas=(config,),
                mock_process=False,
            ),
        )

    mock_agent.assert_called_once()
    called_kwargs = mock_agent.call_args.kwargs
    assert called_kwargs["sender_email"] == "alice@test.com"
    assert called_kwargs["sender_user_id"] == seeded_db["alice_id"]

    written_config = json.loads(artifacts.config_path.read_text())
    assert written_config["process_mode"] == "real"

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    event_names = [event["event"] for event in events]
    assert "sim.process_email_started" in event_names
    assert "sim.process_email_completed" in event_names
    assert "sim.mock_process_email" not in event_names


@pytest.mark.asyncio
async def test_real_process_runs_capture_isolated_traceable_audit_logs(
    tmp_path, capsys
):
    persona_config = PersonaConfig(
        name="Alice",
        email="alice@test.com",
        goal="Find a collaborator for a Rust project.",
        stop_condition="An introduction is made.",
        agent_address="join@example.test",
    )
    adapters = (TinyPersonEmailAdapter(ScriptedTinyPerson("body 1"), persona_config),)
    config = SimRunConfig(
        scenario="real-process",
        ticks=1,
        proactive_every=None,
        personas=(persona_config,),
        mock_process=False,
    )

    async def audited_process(_context, **kwargs):
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        audit_event(
            "agent.tool.completed",
            tool_name="remember",
            trace_id=kwargs["trace_id"],
        )
        audit_model_trace(
            SimpleNamespace(
                all_messages=lambda: [
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="create_event",
                                args={"text": "RAW EVENT CONTENT"},
                            )
                        ]
                    )
                ]
            )
        )

    clock_calls = iter(
        [
            datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 8, 1, 1, 0, tzinfo=timezone.utc),
        ]
    )
    recorder = SimRunRecorder(runs_dir=tmp_path, clock=lambda: next(clock_calls))

    with (
        app.replace_connector(InMemoryConnector()),
        patch("thenetwork.sim.run.recorder.process_email.func", new=audited_process),
    ):
        first = await recorder.run(adapters, config)
        second = await recorder.run(adapters, config)

    for artifacts in (first, second):
        audit_events = [
            json.loads(line) for line in artifacts.audit_path.read_text().splitlines()
        ]
        process_events = [
            json.loads(line)
            for line in artifacts.events_path.read_text().splitlines()
            if "process_email_started" in line
        ]
        completion_events = [
            json.loads(line)
            for line in artifacts.events_path.read_text().splitlines()
            if "process_email_completed" in line
        ]
        assert [event["event"] for event in audit_events] == [
            "agent.tool.completed",
            "agent.model_trace",
        ]
        assert "agent.model_response" not in artifacts.audit_path.read_text()
        assert "RAW EVENT CONTENT" not in artifacts.audit_path.read_text()
        assert process_events[0]["trace_id"] == "[application_identifier]"
        assert len(completion_events) == 1
    assert first.audit_path.read_text() != ""
    assert second.audit_path.read_text() != ""
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_run_recorder_logs_delivery_metadata_without_public_message_bodies(
    tmp_path,
):
    from thenetwork.email.outbound import send_reply

    persona = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Find a collaborator.",
        stop_condition="A connection is made.",
        agent_address="join@example.test",
    )
    settings = MagicMock(
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_account="agent@example.test",
        smtp_password="secret",
        email_from="agent@example.test",
        imap_account="join@example.test",
    )

    async def reply(**kwargs):
        send_reply(
            to_address=kwargs["sender_email"],
            subject="A possible connection",
            body_text="Here is why you may fit.",
        )

    with patch("thenetwork.email.outbound.get_settings", return_value=settings):
        artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
            (TinyPersonEmailAdapter(ScriptedTinyPerson("Inbound details."), persona),),
            SimRunConfig(
                scenario="message-log",
                ticks=1,
                proactive_every=None,
                personas=(persona,),
            ),
            process=reply,
        )

    deliveries = [
        json.loads(line)
        for line in artifacts.events_path.read_text().splitlines()
        if json.loads(line)["event"] == "sim.message_delivered"
    ]

    assert [delivery["direction"] for delivery in deliveries] == [
        "persona->agent",
        "agent->persona",
    ]
    assert [delivery["tick"] for delivery in deliveries] == [1, 1]
    assert deliveries[1]["persona"] is None
    assert deliveries[1]["trace_id"] is None
    canonical_plain_reply = (
        f"Here is why you may fit.\n\n{'\n'.join(standard_signature_lines())}\n"
    )
    assert [
        (delivery["subject"], delivery["body_chars"]) for delivery in deliveries
    ] == [
        ("Simulation tick 1", len("Inbound details.\n")),
        ("A possible connection", len(canonical_plain_reply)),
    ]
    raw_box = mailbox.mbox(artifacts.raw_mbox_path)
    try:
        outbound = next(
            message
            for message in raw_box
            if message.get("Subject") == "A possible connection"
        )
        assert outbound["X-Sim-Tick"] == "1"
        assert outbound["X-Sim-Direction"] == "agent->persona"
        assert outbound.get("X-Sim-Persona") is None
        assert outbound.get("X-Sim-Trace-Id") is None
    finally:
        raw_box.close()
    (outbound_fact,) = tuple(
        fact
        for fact in _mail_facts(artifacts.raw_mbox_path)
        if fact.subject == "A possible connection"
    )
    assert outbound_fact.tick == 1
    assert "Inbound details." not in artifacts.events_path.read_text()
    assert "Here is why you may fit." not in artifacts.events_path.read_text()


@pytest.mark.asyncio
async def test_real_process_run_logs_each_deferred_proactive_trigger(tmp_path):
    persona = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Find a collaborator.",
        stop_condition="A connection is made.",
        agent_address="join@example.test",
    )

    async def opportunity_scan(_timestamp: int) -> None:
        from thenetwork.worker import proactive

        proactive.process_email.defer(
            sender_email="alice@example.test",
            subject="[Proactive] Potential connection",
            body="[System trigger] Opaque candidate id: person-2.",
            trace_id="opportunity-trace",
        )

    async def match_scan(_timestamp: int) -> None:
        from thenetwork.worker import proactive

        proactive.process_email.defer(
            sender_email="alice@example.test",
            subject="[Proactive] New matching signal",
            body="[System match] Person person-1: sanitized gist.",
            trace_id="match-trace",
        )

    async def event_scan(_timestamp: int) -> None:
        from thenetwork.worker import proactive

        proactive.process_email.defer(
            sender_email="alice@example.test",
            subject="[Proactive] Possible event",
            body=(
                "[System event match] Event event-1: RAW EVENT SUBMISSION MUST "
                "NOT REACH PUBLIC ARTIFACTS."
            ),
            proactive_event_id="event-1",
            proactive_event_version=1,
            trace_id="event-trace",
        )

    with (
        app.replace_connector(InMemoryConnector()),
        patch("thenetwork.sim.run.recorder.process_email.func", new=AsyncMock()),
        patch(
            "thenetwork.sim.run.loop.proactive.scan_for_opportunities",
            new=opportunity_scan,
        ),
        patch(
            "thenetwork.sim.run.loop.proactive.scan_for_matches",
            new=match_scan,
        ),
        patch(
            "thenetwork.sim.run.loop.event_scan.scan_for_event_recommendations",
            new=event_scan,
        ),
    ):
        artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
            (TinyPersonEmailAdapter(ScriptedTinyPerson("Inbound details."), persona),),
            SimRunConfig(
                scenario="proactive-trigger-log",
                ticks=1,
                proactive_every=1,
                personas=(persona,),
                mock_process=False,
            ),
        )

    triggers = [
        json.loads(line)
        for line in artifacts.events_path.read_text().splitlines()
        if json.loads(line)["event"] == "sim.proactive_job_deferred"
    ]

    assert triggers == [
        {
            "body": "[System trigger] Opaque candidate id: person-2.",
            "event": "sim.proactive_job_deferred",
            "subject": "[Proactive] Potential connection",
            "trace_id": "opportunity-trace",
            "trigger_kind": "people",
        },
        {
            "body": "[System match] Person person-1: sanitized gist.",
            "event": "sim.proactive_job_deferred",
            "subject": "[Proactive] New matching signal",
            "trace_id": "match-trace",
            "trigger_kind": "people",
        },
        {
            "event": "sim.proactive_job_deferred",
            "event_key": _event_correlation_key("event-1"),
            "event_version": 1,
            "recipient_sender_id_hash": optional_sender_identifier(
                "alice@example.test"
            ),
            "subject": "[Proactive] Possible event",
            "trace_id": "event-trace",
            "trigger_kind": "event",
        },
    ]
    public_events = artifacts.events_path.read_text()
    assert "RAW EVENT SUBMISSION" not in public_events
    assert '"event_id"' not in public_events


@pytest.mark.asyncio
async def test_run_recorder_writes_tier1_score_before_run_completed(tmp_path):
    configs = default_strong_match_configs(agent_address="join@example.test")
    adapters = tuple(
        TinyPersonEmailAdapter(ScriptedTinyPerson(f"body {index}"), config)
        for index, config in enumerate(configs, start=1)
    )
    recorder = SimRunRecorder(runs_dir=tmp_path)

    artifacts = await recorder.run(
        adapters,
        SimRunConfig(
            scenario="strong-match", ticks=1, proactive_every=10, personas=configs
        ),
    )

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    tier1_events = [event for event in events if event["event"] == "sim.score.tier1"]
    assert len(tier1_events) == 1
    assert tier1_events[0]["passed"] is True
    assert events[-1]["event"] == "sim.run_completed"


@pytest.mark.asyncio
async def test_mock_recorder_writes_one_skipped_default_outcome_score(tmp_path):
    config = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Find a collaborator.",
        stop_condition="A connection is made.",
        agent_address="join@example.test",
    )
    artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
        (TinyPersonEmailAdapter(ScriptedTinyPerson("Looking for peers."), config),),
        SimRunConfig(
            scenario="mock-defaults",
            ticks=1,
            proactive_every=None,
            personas=(config,),
            outcome_checks=DEFAULT_OUTCOME_CHECKS,
        ),
    )

    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    outcome_events = [
        event for event in events if event["event"] == "sim.score.outcome"
    ]
    assert len(outcome_events) == 1
    assert len(outcome_events[0]["findings"]) == len(DEFAULT_OUTCOME_CHECKS)
    assert outcome_events[0]["passed"] is True
    assert all(
        finding["evidence"] == {"skipped": True}
        for finding in outcome_events[0]["findings"]
    )


def test_outcome_assembly_reads_fixture_mail_audit_and_database_state(tmp_path):
    artifacts = SimRunArtifacts(
        run_dir=tmp_path,
        config_path=tmp_path / "config.json",
        mbox_path=tmp_path / "all-mail.mbox",
        transcript_path=tmp_path / "transcript.md",
        events_path=tmp_path / "events.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        private_dir=tmp_path / "private",
        raw_mbox_path=tmp_path / "private" / "all-mail.mbox",
        raw_database_dump_path=tmp_path / "private" / "database.dump",
    )
    message = EmailMessage()
    message["From"] = "join@example.test"
    message["To"] = "nadia.sim@example.test"
    message["Subject"] = "A note"
    message["X-Sim-Tick"] = "3"
    message.set_content("Bakery supply co-op update")
    artifacts.private_dir.mkdir()
    SimPostOffice(mbox_path=artifacts.raw_mbox_path).deliver(message)
    artifacts.audit_path.write_text(
        json.dumps({"event": "introduction.consent_transition", "action": "clarify"})
        + "\n",
        encoding="utf-8",
    )
    artifacts.events_path.write_text(
        json.dumps(
            {
                "event": "sim.proactive_job_deferred",
                "trigger_kind": "event",
                "event_key": "evt_v1_test",
                "event_version": 2,
                "recipient_sender_id_hash": "snd_v1_recipient",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rows = (
        IntroductionConsentState(
            person_a_email="omar.sim@example.test",
            person_b_email="peer@example.test",
            status="one_consented",
        ),
    )
    memories = (Memory(id="memory-1", text="raw", refs=["nadia-id"], gist="bakery"),)
    memory_counts = {"nadia.sim@example.test": 1}
    event_rows = (
        EventOutcomeFact(
            event_key="evt_v1_test",
            owner_sender_id_hash="snd_v1_owner",
            version=2,
            active=True,
            recurring=True,
        ),
    )
    event_recommendation_rows = (
        EventRecommendationOutcomeFact(
            event_key="evt_v1_test",
            recipient_sender_id_hash="snd_v1_recipient",
            event_version=2,
            notified=True,
        ),
    )

    emails_by_id = {"nadia-id": "nadia.sim@example.test"}
    with patch(
        "thenetwork.sim.run.recorder._database_outcome_state",
        return_value=(
            rows,
            memories,
            memory_counts,
            emails_by_id,
            event_rows,
            event_recommendation_rows,
        ),
    ):
        outcome, assembled_memories, assembled_emails = _assemble_scenario_outcome(
            artifacts,
            memories=(),
            load_database_state=True,
        )

    assert outcome.consent_rows == rows
    assert outcome.audit_events == (
        {"event": "introduction.consent_transition", "action": "clarify"},
    )
    assert outcome.mail_facts[0].recipients == frozenset({"nadia.sim@example.test"})
    assert outcome.mail_facts[0].body == "Bakery supply co-op update\n"
    assert outcome.mail_facts[0].tick == 3
    assert outcome.memory_counts == memory_counts
    assert outcome.event_rows == event_rows
    assert outcome.event_recommendation_rows == event_recommendation_rows
    assert outcome.proactive_event_triggers == (
        ProactiveEventTriggerOutcomeFact(
            event_key="evt_v1_test",
            recipient_sender_id_hash="snd_v1_recipient",
            event_version=2,
        ),
    )
    assert assembled_memories == memories
    assert assembled_emails == emails_by_id


def test_database_outcome_state_materializes_values_before_session_closes():
    class ExpiringPerson:
        def __init__(self, person_id: str, email: str) -> None:
            self._id = person_id
            self._email = email
            self.detached = False

        @property
        def id(self) -> str:
            if self.detached:
                raise RuntimeError("detached person id")
            return self._id

        @property
        def email(self) -> str:
            if self.detached:
                raise RuntimeError("detached person email")
            return self._email

    class ExpiringMemory:
        def __init__(self) -> None:
            self.detached = False

        def _value(self, name: str, value):
            if self.detached:
                raise RuntimeError(f"detached memory {name}")
            return value

        @property
        def id(self) -> str:
            return self._value("id", "memory-1")

        @property
        def text(self) -> str:
            return self._value("text", "raw")

        @property
        def refs(self) -> list[str]:
            return self._value("refs", ["nadia-id"])

        @property
        def gist(self) -> str:
            return self._value("gist", "bakery")

    nadia = ExpiringPerson("nadia-id", "nadia.sim@example.test")
    peer = ExpiringPerson("peer-id", "peer@example.test")
    memory = ExpiringMemory()
    consent = SimpleNamespace(
        person_a_id="nadia-id",
        person_b_id="peer-id",
        status="introduced",
        person_a_consented=True,
        person_b_consented=True,
    )
    future = datetime.now(timezone.utc) + timedelta(days=30)
    event_row = ("event-1", "nadia-id", 2, future, None, True)
    recommendation_row = ("event-1", "peer-id", 2, future)

    class Result:
        def __init__(self, rows) -> None:
            self.rows = rows

        def all(self):
            return self.rows

    class Session:
        def exec(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            return Result(
                {
                    IntroductionConsent: [consent],
                    Memory: [memory],
                    Person: [nadia, peer],
                    Event: [event_row],
                    EventRecommendation: [recommendation_row],
                }[entity]
            )

        def get(self, model, row_id):
            assert model is Person
            return {"nadia-id": nadia, "peer-id": peer}[row_id]

    @contextmanager
    def session_context():
        try:
            yield Session()
        finally:
            nadia.detached = True
            peer.detached = True
            memory.detached = True

    with (
        patch("thenetwork.sim.run.recorder.get_session", session_context),
        patch(
            "thenetwork.sim.run.recorder.optional_sender_identifier",
            side_effect=lambda email: f"sender-key:{email}",
        ),
    ):
        (
            consent_rows,
            memories,
            memory_counts,
            emails_by_id,
            event_rows,
            event_recommendation_rows,
        ) = _database_outcome_state()

    assert consent_rows[0].participant_emails == frozenset(
        {"nadia.sim@example.test", "peer@example.test"}
    )
    assert consent_rows[0].both_consented is True
    assert memories[0].gist == "bakery"
    assert memories[0].refs == ["nadia-id"]
    assert memory_counts == {"nadia.sim@example.test": 1}
    assert emails_by_id == {
        "nadia-id": "nadia.sim@example.test",
        "peer-id": "peer@example.test",
    }
    assert event_rows == (
        EventOutcomeFact(
            event_key=_event_correlation_key("event-1"),
            owner_sender_id_hash="sender-key:nadia.sim@example.test",
            version=2,
            active=True,
            recurring=True,
        ),
    )
    assert event_recommendation_rows == (
        EventRecommendationOutcomeFact(
            event_key=_event_correlation_key("event-1"),
            recipient_sender_id_hash="sender-key:peer@example.test",
            event_version=2,
            notified=True,
        ),
    )


def _fake_git_run(commit: str, porcelain: str):
    def fake_run(args, **_kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout=f"{commit}\n")
        if args[:2] == ["git", "status"]:
            return SimpleNamespace(stdout=porcelain)
        raise AssertionError(f"unexpected git invocation: {args}")

    return fake_run


def test_config_payload_records_clean_git_commit():
    config = SimRunConfig(
        scenario="provenance",
        ticks=1,
        proactive_every=None,
        personas=(),
    )

    with patch(
        "thenetwork.sim.run.recorder.subprocess.run",
        side_effect=_fake_git_run("abc123", ""),
    ):
        payload = _config_payload(config, "mock")

    assert payload["git"] == {"commit": "abc123", "dirty": False}


def test_config_payload_records_dirty_tree():
    config = SimRunConfig(
        scenario="provenance",
        ticks=1,
        proactive_every=None,
        personas=(),
    )

    with patch(
        "thenetwork.sim.run.recorder.subprocess.run",
        side_effect=_fake_git_run("abc123", " M thenetwork/sim/run/recorder.py\n"),
    ):
        payload = _config_payload(config, "mock")

    assert payload["git"] == {"commit": "abc123", "dirty": True}


def test_config_payload_git_provenance_fails_closed_to_none_when_git_unavailable():
    config = SimRunConfig(
        scenario="provenance",
        ticks=1,
        proactive_every=None,
        personas=(),
    )

    with patch(
        "thenetwork.sim.run.recorder.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        payload = _config_payload(config, "mock")

    assert payload["git"] == {"commit": None, "dirty": None}


def _runtime_settings():
    return SimpleNamespace(
        agent_model="anthropic:claude-sonnet-5",
        sanitize_model="openai/privacy-filter",
        small_agent_model="anthropic:claude-haiku-4-5",
        embed_model="text-embedding-3-small",
        agent_thinking_level="high",
        agent_request_limit=7,
        agent_total_tokens_limit=4321,
        model_request_timeout_seconds=45.5,
        agent_api_key="agent-secret-value",
        small_agent_api_key="small-secret-value",
        embed_api_key="embed-secret-value",
        postgres_password="database-secret-value",
    )


@pytest.mark.parametrize(
    ("process_mode", "llm_personas", "active_roles"),
    [
        ("mock", False, set()),
        ("mock", True, {"persona"}),
        ("real", False, {"agent", "sanitizer", "embedding"}),
        ("real", True, {"agent", "persona", "sanitizer", "embedding"}),
    ],
)
def test_runtime_provenance_records_models_settings_and_active_modes(
    process_mode, llm_personas, active_roles
):
    config = SimRunConfig(
        scenario="runtime-provenance",
        ticks=1,
        proactive_every=None,
        personas=(),
        mock_process=process_mode != "real",
        llm_personas=llm_personas,
    )

    with patch(
        "thenetwork.sim.run.recorder.get_settings",
        return_value=_runtime_settings(),
    ):
        provenance = _config_payload(config, process_mode)["runtime_provenance"]

    assert provenance["version"] == 1
    assert {
        role for role, model in provenance["models"].items() if model["active"]
    } == active_roles
    assert provenance["models"] == {
        "agent": {
            "identifier": "anthropic:claude-sonnet-5",
            "active": "agent" in active_roles,
        },
        "persona": {
            "identifier": "anthropic:claude-haiku-4-5",
            "active": "persona" in active_roles,
        },
        "sanitizer": {
            "identifier": "openai/privacy-filter",
            "active": "sanitizer" in active_roles,
        },
        "embedding": {
            "identifier": "text-embedding-3-small",
            "active": "embedding" in active_roles,
        },
    }
    assert provenance["settings"] == {
        "agent_thinking_level": "high",
        "agent_request_limit": 7,
        "agent_total_tokens_limit": 4321,
        "model_request_timeout_seconds": 45.5,
        "sanitizer_mode": "privacy-filter",
    }


def test_runtime_provenance_hashes_only_static_prompt_templates(tmp_path):
    private_persona = PersonaConfig(
        name="Private Persona Name",
        email="private-persona@example.test",
        goal="Private owner goal text",
        stop_condition="Private stop condition text",
        agent_address="private-agent@example.test",
    )
    config = SimRunConfig(
        scenario="runtime-provenance",
        ticks=1,
        proactive_every=None,
        personas=(private_persona,),
        llm_personas=True,
    )

    with patch(
        "thenetwork.sim.run.recorder.get_settings",
        return_value=_runtime_settings(),
    ):
        provenance = _config_payload(config, "mock")["runtime_provenance"]

    assert provenance["static_prompt_sha256"] == {
        "agent_known_sender": hashlib.sha256(
            SYSTEM_PROMPTS[KNOWN_SENDER].encode("utf-8")
        ).hexdigest(),
        "agent_first_contact": hashlib.sha256(
            SYSTEM_PROMPTS[FIRST_CONTACT].encode("utf-8")
        ).hexdigest(),
        "agent_people_trigger": hashlib.sha256(
            SYSTEM_PROMPTS[PEOPLE_TRIGGER].encode("utf-8")
        ).hexdigest(),
        "agent_event_trigger": hashlib.sha256(
            SYSTEM_PROMPTS[EVENT_TRIGGER].encode("utf-8")
        ).hexdigest(),
        "persona_template": hashlib.sha256(_PERSONA_PROMPT.encode("utf-8")).hexdigest(),
    }
    assert provenance["settings"]["sanitizer_mode"] == "privacy-filter"
    assert provenance["models"]["sanitizer"]["active"] is False

    serialized = json.dumps(provenance, sort_keys=True)
    for private_value in (
        "Private Persona Name",
        "private-persona@example.test",
        "Private owner goal text",
        "Private stop condition text",
        "agent-secret-value",
        "small-secret-value",
        "embed-secret-value",
        "database-secret-value",
        *SYSTEM_PROMPTS.values(),
        _PERSONA_PROMPT,
    ):
        assert private_value not in serialized

    path = tmp_path / "config.json"
    write_redacted_json(path, {"runtime_provenance": provenance})
    assert json.loads(path.read_text())["runtime_provenance"] == provenance

    provenance["models"]["agent"]["identifier"] = "sk-secret-model-setting"
    write_redacted_json(path, {"runtime_provenance": provenance})
    assert "sk-secret-model-setting" not in path.read_text()


def test_static_prompt_hashes_survive_recognizer_false_positives(tmp_path) -> None:
    """A digest must round-trip whatever hex the current prompt text produces.

    The span classifier can label an identifier-shaped run inside a random hex
    digest, which would silently corrupt the provenance anchor tying a run to
    the prompt that produced it. This digest is a known false-positive trigger,
    so it pins the exemption rather than relying on the live prompt hashing to
    something the classifier happens to ignore.
    """
    path = tmp_path / "config.json"
    tripping_digest = "1f77f6528ed8d4472abdd2396d89c421edeff6dddee2659374eaad6d69edf213"
    provenance = {"static_prompt_sha256": {"agent": tripping_digest}}

    write_redacted_json(path, {"runtime_provenance": provenance})
    written = json.loads(path.read_text())["runtime_provenance"]

    assert written["static_prompt_sha256"]["agent"] == tripping_digest


def test_static_prompt_hash_slot_redacts_a_non_digest_value(tmp_path) -> None:
    """The exemption is shape-bound: only a real digest may pass through it."""
    path = tmp_path / "config.json"
    provenance = {
        "static_prompt_sha256": {"agent": "sk-secret-smuggled-through-the-hash-slot"}
    }

    write_redacted_json(path, {"runtime_provenance": provenance})
    written = json.loads(path.read_text())["runtime_provenance"]

    assert written["static_prompt_sha256"]["agent"] == "[redacted]"
    assert "sk-secret-smuggled-through-the-hash-slot" not in path.read_text()


@pytest.mark.asyncio
async def test_recording_process_records_override_failure_without_retry(tmp_path):
    events = EventsLog(tmp_path / "events.jsonl")
    calls = []

    async def failing_process(**kwargs):
        calls.append(kwargs)
        raise ModelHTTPError(status_code=503, model_name="test-model")

    wrapped = _recording_process(failing_process, events)
    await wrapped(sender_email="alice@example.test", trace_id="trace-1")

    assert len(calls) == 1
    logged = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    event_names = [entry["event"] for entry in logged]
    assert "sim.process_email_failed" in event_names
    assert "sim.process_email_completed" not in event_names


def test_simulation_job_drainer_records_terminal_job_outcomes(tmp_path):
    events = EventsLog(tmp_path / "events.jsonl")
    drainer = _SimulationJobDrainer(events)
    job_kwargs = {
        "sender_email": "alice@example.test",
        "trace_id": "trace-1",
    }

    drainer._record_job_outcomes(
        (
            SimpleNamespace(
                id=1,
                status="succeeded",
                attempts=1,
                task_kwargs=job_kwargs,
            ),
            SimpleNamespace(
                id=2,
                status="failed",
                attempts=3,
                task_kwargs=job_kwargs,
            ),
        )
    )

    logged = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert [entry["event"] for entry in logged] == [
        "sim.process_email_completed",
        "sim.process_email_retrying",
        "sim.process_email_retrying",
        "sim.process_email_failed",
    ]
    terminal = [entry for entry in logged if "job_status" in entry]
    assert [entry["attempts"] for entry in terminal] == [1, 3]
    assert [entry["job_status"] for entry in terminal] == ["succeeded", "failed"]
    retries = [entry for entry in logged if entry["event"].endswith("retrying")]
    assert [entry["attempt"] for entry in retries] == [1, 2]


def test_simulation_job_drainer_does_not_duplicate_observed_retry(tmp_path):
    events = EventsLog(tmp_path / "events.jsonl")
    drainer = _SimulationJobDrainer(events)
    job_kwargs = {
        "sender_email": "alice@example.test",
        "trace_id": "trace-1",
    }
    pending = SimpleNamespace(
        id=1,
        status="todo",
        attempts=1,
        task_kwargs=job_kwargs,
    )
    terminal = SimpleNamespace(
        id=1,
        status="succeeded",
        attempts=2,
        task_kwargs=job_kwargs,
    )

    drainer._record_retries((pending,))
    drainer._record_job_outcomes((terminal,))

    logged = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert [entry["event"] for entry in logged] == [
        "sim.process_email_retrying",
        "sim.process_email_completed",
    ]
    assert logged[-1]["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_ids", "listed_jobs"),
    [
        pytest.param(set(), [], id="no_tracked_jobs"),
        pytest.param(
            {1},
            [
                SimpleNamespace(
                    id=1,
                    status="succeeded",
                    attempts=1,
                    task_kwargs={
                        "sender_email": "alice@example.test",
                        "trace_id": "trace-1",
                    },
                )
            ],
            id="only_terminal_jobs",
        ),
    ],
)
async def test_simulation_job_drainer_skips_worker_when_no_work_is_ready(
    tmp_path, job_ids, listed_jobs
):
    events = EventsLog(tmp_path / "events.jsonl")
    drainer = _SimulationJobDrainer(events, job_ids=job_ids)

    with (
        patch.object(app, "run_worker_async", new_callable=AsyncMock) as run_worker,
        patch.object(
            app.job_manager,
            "list_jobs_async",
            new_callable=AsyncMock,
            return_value=listed_jobs,
        ),
    ):
        await drainer()

    run_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_simulation_job_drainer_runs_ready_work_with_one_shot_options(tmp_path):
    events = EventsLog(tmp_path / "events.jsonl")
    drainer = _SimulationJobDrainer(events, concurrency=3, job_ids={1})
    job_kwargs = {"sender_email": "alice@example.test", "trace_id": "trace-1"}
    ready = SimpleNamespace(
        id=1,
        status="todo",
        attempts=0,
        scheduled_at=None,
        task_kwargs=job_kwargs,
    )
    terminal = SimpleNamespace(
        id=1,
        status="succeeded",
        attempts=1,
        task_kwargs=job_kwargs,
    )

    with (
        patch.object(app, "run_worker_async", new_callable=AsyncMock) as run_worker,
        patch.object(
            app.job_manager,
            "list_jobs_async",
            new_callable=AsyncMock,
            side_effect=([ready], [terminal]),
        ),
    ):
        await drainer()

    run_worker.assert_awaited_once_with(
        queues=["simulation_process_email"],
        concurrency=3,
        wait=False,
        listen_notify=False,
        install_signal_handlers=False,
    )


@pytest.mark.asyncio
async def test_real_process_retries_non_5xx_model_http_error_via_procrastinate(
    tmp_path, monkeypatch
):
    connector = InMemoryConnector()
    calls = []
    now = [datetime(2026, 7, 14, tzinfo=timezone.utc)]

    async def advance_retry_clock(seconds: float) -> None:
        now[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(procrastinate_utils, "utcnow", lambda: now[0])
    monkeypatch.setattr(
        "thenetwork.sim.run.recorder._sleep_until_retry",
        advance_retry_clock,
    )

    async def flaky_process(_context, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ModelHTTPError(status_code=400, model_name="test-model")

    persona = PersonaConfig(
        name="Alice",
        email="alice@example.test",
        goal="Find a collaborator.",
        stop_condition="Wait for a match.",
        agent_address="join@example.test",
    )
    production_strategy = process_email.retry_strategy
    assert production_strategy is not None
    assert production_strategy.wait == 60
    with (
        app.replace_connector(connector),
        patch.object(process_email, "func", new=flaky_process),
    ):
        artifacts = await SimRunRecorder(runs_dir=tmp_path).run(
            (TinyPersonEmailAdapter(ScriptedTinyPerson("Hello"), persona),),
            SimRunConfig(
                scenario="queue-retry",
                ticks=1,
                proactive_every=None,
                personas=(persona,),
                mock_process=False,
            ),
        )

    assert len(calls) == 2
    assert process_email.retry_strategy is production_strategy
    sim_jobs = [
        job
        for job in connector.jobs.values()
        if job["queue_name"] == "simulation_process_email"
    ]
    assert [job["status"] for job in sim_jobs] == ["succeeded"]
    events = [
        json.loads(line) for line in artifacts.events_path.read_text().splitlines()
    ]
    event_names = [event["event"] for event in events]
    assert event_names.count("sim.process_email_started") == 1
    assert event_names.count("sim.process_email_retrying") == 1
    assert event_names.count("sim.process_email_completed") == 1
    assert "sim.process_email_failed" not in event_names


def test_config_payload_keeps_outcome_check_metadata_without_predicates():
    config = SimRunConfig(
        scenario="metadata",
        ticks=1,
        proactive_every=None,
        personas=(),
        outcome_checks=(
            OutcomeCheck(
                description="callable-free metadata",
                predicate=lambda _outcome: True,
                requires_real_process=True,
            ),
        ),
    )

    payload = _config_payload(config, "mock")

    assert payload["outcome_checks"] == [
        {
            "description": "callable-free metadata",
            "requires_real_process": True,
            "requires_llm_personas": False,
        }
    ]


@pytest.mark.asyncio
async def test_run_recorder_does_not_multiply_or_drop_outcome_metrics(tmp_path):
    persona_config = PersonaConfig(
        name="Alice",
        email="alice@test.com",
        goal="Find a collaborator for a Rust project.",
        stop_condition="An introduction is made.",
        agent_address="join@example.test",
    )
    adapters = (TinyPersonEmailAdapter(ScriptedTinyPerson("body 1"), persona_config),)
    config = SimRunConfig(
        scenario="strong-match",
        ticks=1,
        proactive_every=None,
        personas=(persona_config,),
    )

    async def two_tool_calls(**_kwargs):
        return {
            "tool_calls": ("remember", "send_outreach"),
            "total_tokens": 100,
            "cost_usd": 0.02,
        }

    async def zero_tool_calls_with_tokens(**_kwargs):
        return {"tool_calls": (), "total_tokens": 50, "cost_usd": 0.01}

    recorder = SimRunRecorder(runs_dir=tmp_path)
    multi_tool = await recorder.run(adapters, config, process=two_tool_calls)
    zero_tool = await recorder.run(
        adapters, config, process=zero_tool_calls_with_tokens
    )

    def totals(run_dir) -> tuple[int, float]:
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        return (
            sum(int(event.get("total_tokens") or 0) for event in events),
            sum(float(event.get("cost_usd") or 0.0) for event in events),
        )

    multi_tokens, multi_cost = totals(multi_tool.run_dir)
    zero_tokens, zero_cost = totals(zero_tool.run_dir)

    assert multi_tokens == 100
    assert multi_cost == pytest.approx(0.02)
    assert zero_tokens == 50
    assert zero_cost == pytest.approx(0.01)
