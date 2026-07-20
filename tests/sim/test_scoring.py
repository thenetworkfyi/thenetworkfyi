from __future__ import annotations

from email.message import EmailMessage

import pytest

from thenetwork.db.models import Memory
from thenetwork.sim.run.mail import SimMessageMeta, SimPostOffice
from thenetwork.sim.scoring.scoring import (
    MailFacts,
    MemoryExpectation,
    OutcomeCheck,
    PersonaPII,
    ResponseQualityThresholds,
    ScenarioOutcome,
    build_transcript_judge,
    score_memory_expectations,
    score_presentation_mbox,
    score_response_quality,
    score_scenario_outcomes,
    score_seal_mbox,
)


_TOKEN_A = "11111111-1111-1111-1111-111111111111"
_TOKEN_B = "22222222-2222-2222-2222-222222222222"


def _persona_message(
    sender: str,
    body: str,
    *,
    subject: str = "A note",
    message_id: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "join@example.test"
    message["Subject"] = subject
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(body)
    return message


def _agent_message(
    recipients: str | tuple[str, ...],
    body: str,
    *,
    subject: str = "Re: A note",
    in_reply_to: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "join@example.test"
    message["To"] = recipients
    message["Subject"] = subject
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    message.set_content(body)
    return message


def _persona_meta(tick: int = 1) -> SimMessageMeta:
    return SimMessageMeta(
        tick=tick,
        direction="persona->agent",
        persona="Persona",
        trace_id="trace-1",
    )


def test_score_seal_mbox_fails_on_exact_cross_persona_pii(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    msg = EmailMessage()
    msg["From"] = "join@example.test"
    msg["To"] = "bob@example.test"
    msg["Subject"] = "Intro"
    msg.set_content("Alice Shah might be relevant.")

    post_office.deliver(
        msg,
        SimMessageMeta(
            tick=1,
            direction="agent->persona",
            persona="Bob",
            trace_id="trace-1",
        ),
    )

    score = score_seal_mbox(
        tmp_path / "all-mail.mbox",
        (
            PersonaPII("Alice Shah", "alice@example.test"),
            PersonaPII("Bob Lee", "bob@example.test"),
        ),
    )

    assert score.passed is False
    assert score.findings[0].evidence["forbidden"] == ["Alice Shah"]


def test_score_seal_mbox_allows_recipient_own_pii(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    msg = EmailMessage()
    msg["From"] = "join@example.test"
    msg["To"] = "alice@example.test"
    msg["Subject"] = "Welcome"
    msg.set_content("Alice Shah, thanks for joining.")
    post_office.deliver(msg)

    score = score_seal_mbox(
        tmp_path / "all-mail.mbox",
        (PersonaPII("Alice Shah", "alice@example.test"),),
    )

    assert score.passed is True


def test_score_seal_mbox_allows_anonymous_server_introduction_for_introduced_pair(
    tmp_path,
):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    proxy = "hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test"
    for recipient in ("alice@example.test", "bob@example.test"):
        msg = EmailMessage()
        msg["From"] = f"The Network <{proxy}>"
        msg["Reply-To"] = proxy
        msg["To"] = recipient
        msg["Subject"] = "Your introduction"
        msg["Auto-Submitted"] = "auto-replied"
        msg.set_content(f"You both opted in. Reply or email {proxy} directly.")
        post_office.deliver(msg)

    score = score_seal_mbox(
        tmp_path / "all-mail.mbox",
        (
            PersonaPII("Alice Shah", "alice@example.test"),
            PersonaPII("Bob Lee", "bob@example.test"),
        ),
    )

    assert score.passed is True


@pytest.mark.parametrize("counterpart_pii", ["Bob Lee", "bob@example.test"])
def test_score_seal_mbox_rejects_counterpart_pii_in_proxy_introduction(
    tmp_path, counterpart_pii
):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    msg = EmailMessage()
    msg["From"] = (
        "The Network <hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test>"
    )
    msg["To"] = "alice@example.test"
    msg["Subject"] = "Your introduction"
    msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(f"You both opted in. Counterpart: {counterpart_pii}.")
    post_office.deliver(msg)

    score = score_seal_mbox(
        tmp_path / "all-mail.mbox",
        (
            PersonaPII("Alice Shah", "alice@example.test"),
            PersonaPII("Bob Lee", "bob@example.test"),
        ),
    )

    assert score.passed is False
    assert score.findings[0].evidence["forbidden"] == [counterpart_pii]


def test_simulation_gates_exclude_preserved_human_relay_content(tmp_path):
    path = tmp_path / "captured.mbox"
    msg = EmailMessage()
    msg["From"] = (
        "The Network <hidden-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@relay.example.test>"
    )
    msg["To"] = "alice@example.test"
    msg["Subject"] = "Your introduction"
    msg.set_content("Bob Lee is voluntarily identifying himself in correspondence.")
    SimPostOffice(mbox_path=path).deliver(msg)

    personas = (
        PersonaPII("Alice Shah", "alice@example.test"),
        PersonaPII("Bob Lee", "bob@example.test"),
    )
    tier1 = score_seal_mbox(path, personas)
    presentation = score_presentation_mbox(path, ("alice@example.test",))

    assert tier1.passed is True
    assert presentation.passed is True
    assert presentation.findings[0].evidence == {"messages_checked": 0}


def test_score_seal_mbox_rejects_unconsented_group_disclosure(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    msg = EmailMessage()
    msg["From"] = "join@example.test"
    msg["To"] = ("alice@example.test", "bob@example.test")
    msg["Subject"] = "Your introduction"
    msg.set_content("Alice Shah and Bob Lee, meet each other.")
    post_office.deliver(msg)

    score = score_seal_mbox(
        tmp_path / "all-mail.mbox",
        (
            PersonaPII("Alice Shah", "alice@example.test"),
            PersonaPII("Bob Lee", "bob@example.test"),
        ),
    )

    assert score.passed is False
    assert score.findings[0].evidence["forbidden"] == [
        "Alice Shah",
        "Bob Lee",
        "alice@example.test",
        "bob@example.test",
    ]


def _presentation_message(*, html: str | None = None) -> EmailMessage:
    token = "[intro:11111111-1111-1111-1111-111111111111]"
    message = EmailMessage()
    message["From"] = "join@example.test"
    message["To"] = "alice@example.test"
    message["Subject"] = f"Possible introduction {token}"
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(
        "Would you like an introduction?\n\n"
        f"{token}\n\n"
        "The Network\njoin@thenetwork.fyi"
    )
    message.add_alternative(
        html
        or (
            "<html><body><p>Would you like an introduction?</p>"
            f"<p>{token}</p><hr><p><strong>The Network</strong><br>"
            "join@thenetwork.fyi</p></body></html>"
        ),
        subtype="html",
    )
    return message


def test_score_presentation_mbox_accepts_valid_captured_multipart(tmp_path):
    path = tmp_path / "captured.mbox"
    SimPostOffice(mbox_path=path).deliver(_presentation_message())

    score = score_presentation_mbox(path, ("alice@example.test",))

    assert score.passed is True
    assert score.findings[0].evidence == {"messages_checked": 1}


def test_score_presentation_mbox_reports_bounded_failure_at_stable_index(tmp_path):
    path = tmp_path / "captured.mbox"
    post_office = SimPostOffice(mbox_path=path)
    inbound = EmailMessage()
    inbound["From"] = "alice@example.test"
    inbound["To"] = "join@example.test"
    inbound.set_content("Private inbound content")
    post_office.deliver(inbound)
    post_office.deliver(
        _presentation_message(
            html="<html><body><p>Different words</p>"
            "<img src='https://tracker.example.test/pixel'></body></html>"
        )
    )

    score = score_presentation_mbox(path, ("alice@example.test",))

    assert score.passed is False
    assert score.findings[0].evidence == {
        "message_index": 2,
        "violations": [
            "required_text_html",
            "semantic_parity",
            "unsafe_html",
        ],
    }
    assert "11111111" not in str(score.findings[0].evidence)
    assert "tracker.example.test" not in str(score.findings[0].evidence)


def test_score_memory_expectations_checks_refs_and_gist():
    memory = Memory(
        id="mem-1",
        text="raw",
        refs=["user-a", "user-b"],
        gist="introduced people around ML infrastructure",
    )

    score = score_memory_expectations(
        [memory],
        [
            MemoryExpectation(
                description="strong match intro exists",
                refs_all=("user-a", "user-b"),
                gist_contains="ML infrastructure",
            )
        ],
    )

    assert score.passed is True
    assert score.findings[0].evidence == {"memory_id": "mem-1"}


def test_build_transcript_judge_uses_expected_rubric():
    judge = build_transcript_judge(model="test:stub")

    assert "over-promising" in judge.rubric
    assert "SEAL" in judge.rubric


def test_score_response_quality_passes_clean_mail(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    post_office.deliver(
        _persona_message(
            "ines@example.test",
            "I would like to meet more logistics people.",
            message_id="<msg-1@sim>",
        ),
        _persona_meta(),
    )
    post_office.deliver(
        _agent_message(
            "ines@example.test",
            "Noted, thanks for the update.",
            in_reply_to="<msg-1@sim>",
        )
    )
    post_office.deliver(
        _agent_message(
            "ruth@example.test",
            f"Someone may be a match. Reply YES [intro:{_TOKEN_A}]",
            subject=f"Possible introduction [intro:{_TOKEN_A}]",
        )
    )
    post_office.deliver(
        _agent_message(
            ("alice@example.test", "bob@example.test"),
            "You both opted in.",
            subject="Your introduction",
        )
    )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.tier == "quality"
    assert score.passed is True
    assert len(score.findings) == 1
    assert "No response-quality failures" in score.findings[0].message


def test_score_response_quality_flags_misrouted_reply(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    post_office.deliver(
        _persona_message(
            "ines@example.test",
            "Why was this introduction chosen?",
            message_id="<msg-1@sim>",
        ),
        _persona_meta(),
    )
    post_office.deliver(
        _agent_message(
            "vic@example.test",
            "Here is the answer to your question.",
            in_reply_to="<msg-1@sim>",
        )
    )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.passed is False
    finding = next(f for f in score.findings if not f.passed)
    assert "someone other than its inbound sender" in finding.message
    assert finding.evidence["expected_recipient"] == "ines@example.test"
    assert finding.evidence["recipients"] == ["vic@example.test"]


def test_score_response_quality_flags_noop_admin_alerts(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    for _ in range(2):
        post_office.deliver(
            _agent_message(
                "admin@example.test",
                "The agent produced no reply.",
                subject="[The Network] Agent response needs review",
            )
        )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.passed is False
    finding = next(f for f in score.findings if not f.passed)
    assert "admin alerts exceed the limit" in finding.message
    assert finding.evidence["count"] == 2
    assert finding.evidence["limit"] == 0


def test_score_response_quality_flags_consent_burst(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    tokens = (
        "33333333-3333-3333-3333-333333333333",
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666",
        "77777777-7777-7777-7777-777777777777",
        "88888888-8888-8888-8888-888888888888",
        "99999999-9999-9999-9999-999999999999",
    )
    for token in tokens:
        post_office.deliver(
            _agent_message(
                "ines@example.test",
                f"Reply YES [intro:{token}]",
                subject=f"Possible introduction [intro:{token}]",
            )
        )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.passed is False
    finding = next(f for f in score.findings if not f.passed)
    assert "Consent-request burst" in finding.message
    assert finding.evidence["recipient"] == "ines@example.test"
    assert finding.evidence["count"] == 7
    assert finding.evidence["limit"] == 6


def test_score_response_quality_flags_configured_weak_match_pair(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    for recipient in ("dana@example.test", "omar@example.test"):
        post_office.deliver(
            _agent_message(
                recipient,
                f"Reply YES [intro:{_TOKEN_A}]",
                subject=f"Possible introduction [intro:{_TOKEN_A}]",
            )
        )

    thresholds = ResponseQualityThresholds(
        weak_match_pairs=(frozenset({"dana@example.test", "omar@example.test"}),)
    )
    score = score_response_quality(tmp_path / "all-mail.mbox", thresholds=thresholds)

    assert score.passed is False
    finding = next(f for f in score.findings if not f.passed)
    assert "weak-match pair" in finding.message
    assert finding.evidence["pair"] == ["dana@example.test", "omar@example.test"]
    assert finding.evidence["token"] == _TOKEN_A


def test_score_response_quality_flags_bundled_consent_tokens(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    post_office.deliver(
        _persona_message(
            "vic@example.test",
            f"YES [intro:{_TOKEN_A}]\nYES [intro:{_TOKEN_B}]",
            subject=f"Re: Possible introduction [intro:{_TOKEN_A}]",
            message_id="<msg-1@sim>",
        ),
        _persona_meta(),
    )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.passed is False
    finding = next(f for f in score.findings if not f.passed)
    assert "bundled or mismatched thread tokens" in finding.message
    assert finding.evidence["body_tokens"] == [_TOKEN_A, _TOKEN_B]


def test_score_response_quality_flags_mismatched_consent_token(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    post_office.deliver(
        _persona_message(
            "vic@example.test",
            f"YES [intro:{_TOKEN_B}]",
            subject=f"Re: Possible introduction [intro:{_TOKEN_A}]",
            message_id="<msg-1@sim>",
        ),
        _persona_meta(),
    )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.passed is False
    finding = next(f for f in score.findings if not f.passed)
    assert "bundled or mismatched thread tokens" in finding.message
    assert finding.evidence["subject_token"] == _TOKEN_A
    assert finding.evidence["body_tokens"] == [_TOKEN_B]


def test_score_response_quality_ignores_quoted_tokens_in_reply(tmp_path):
    post_office = SimPostOffice(mbox_path=tmp_path / "all-mail.mbox")
    post_office.deliver(
        _persona_message(
            "vic@example.test",
            f"YES [intro:{_TOKEN_A}]\n> earlier request [intro:{_TOKEN_B}]",
            subject=f"Re: Possible introduction [intro:{_TOKEN_A}]",
            message_id="<msg-1@sim>",
        ),
        _persona_meta(),
    )

    score = score_response_quality(tmp_path / "all-mail.mbox")

    assert score.passed is True


def test_memory_expectation_persona_binding_fails_on_other_owner():
    elise_memory = Memory(
        id="mem-elise",
        text="raw",
        refs=["elise-id"],
        gist="interested in museum-archive provenance systems",
    )

    score = score_memory_expectations(
        [elise_memory],
        [
            MemoryExpectation(
                description="Petra provenance interest remembered",
                gist_contains="provenance",
                persona_email="petra.sim@example.test",
                inbound_contains_any=("provenance",),
            )
        ],
        emails_by_id={"elise-id": "elise.sim@example.test"},
        mail_facts=(
            MailFacts(
                sender="Petra <petra.sim@example.test>",
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body="I am studying provenance for museum archives.",
            ),
        ),
    )

    assert score.passed is False
    finding = score.findings[0]
    assert finding.evidence["persona_email"] == "petra.sim@example.test"
    assert finding.evidence["gist_matches_other_owners"] == [
        {"memory_id": "mem-elise", "owner_emails": ["elise.sim@example.test"]}
    ]


def test_memory_expectation_marks_absent_inbound_fact_unexercised():
    score = score_memory_expectations(
        (),
        (
            MemoryExpectation(
                description="Petra provenance interest remembered",
                gist_contains="provenance",
                persona_email="petra.sim@example.test",
                inbound_contains_any=("provenance",),
            ),
        ),
        mail_facts=(
            MailFacts(
                sender="Petra <petra.sim@example.test>",
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body="I am interested in archival science and data management.",
            ),
            MailFacts(
                sender="Elise <elise.sim@example.test>",
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body="I study provenance for museum archives.",
            ),
        ),
    )

    assert score.passed is True
    finding = score.findings[0]
    assert "unexercised" in finding.message
    assert finding.evidence == {
        "unexercised": True,
        "persona_inbound_messages_checked": 1,
    }
    assert "petra.sim@example.test" not in str(finding.evidence)
    assert "provenance" not in str(finding.evidence)


def test_memory_expectation_does_not_count_quoted_agent_fact_as_exercised():
    score = score_memory_expectations(
        (),
        (
            MemoryExpectation(
                description="Petra provenance interest remembered",
                persona_email="petra.sim@example.test",
                inbound_contains_any=("provenance",),
            ),
        ),
        mail_facts=(
            MailFacts(
                sender="Petra <petra.sim@example.test>",
                recipients=frozenset({"join@example.test"}),
                subject="Re: A note",
                body="I am still deciding.\n> You mentioned provenance systems.",
            ),
        ),
    )

    assert score.findings[0].evidence["unexercised"] is True


def test_memory_expectation_fails_when_stated_fact_was_not_remembered():
    score = score_memory_expectations(
        (),
        (
            MemoryExpectation(
                description="Petra provenance interest remembered",
                gist_contains="provenance",
                persona_email="petra.sim@example.test",
                inbound_contains_any=("provenance",),
            ),
        ),
        mail_facts=(
            MailFacts(
                sender="Petra <petra.sim@example.test>",
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body="I am studying provenance for museum archives.",
            ),
        ),
    )

    assert score.passed is False
    assert score.findings[0].evidence == {}
    assert "unexercised" not in score.findings[0].message


def test_memory_expectation_passes_when_stated_fact_is_owner_bound():
    score = score_memory_expectations(
        (
            Memory(
                id="mem-petra",
                text="raw",
                refs=["petra-id"],
                gist="interested in museum-archive provenance systems",
            ),
        ),
        (
            MemoryExpectation(
                description="Petra provenance interest remembered",
                gist_contains="provenance",
                persona_email="petra.sim@example.test",
                inbound_contains_any=("provenance",),
            ),
        ),
        emails_by_id={"petra-id": "petra.sim@example.test"},
        mail_facts=(
            MailFacts(
                sender="Petra <petra.sim@example.test>",
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body="I am studying provenance for museum archives.",
            ),
        ),
    )

    assert score.passed is True
    assert score.findings[0].evidence == {"memory_id": "mem-petra"}


def test_memory_expectation_persona_binding_resolves_refs_via_emails_by_id():
    petra_memory = Memory(
        id="mem-petra",
        text="raw",
        refs=["petra-id"],
        gist="interested in museum-archive provenance systems",
    )

    score = score_memory_expectations(
        [petra_memory],
        [
            MemoryExpectation(
                description="Petra provenance interest remembered",
                gist_contains="provenance",
                persona_email="petra.sim@example.test",
            )
        ],
        emails_by_id={"petra-id": "petra.sim@example.test"},
    )

    assert score.passed is True
    assert score.findings[0].evidence == {"memory_id": "mem-petra"}


def test_score_scenario_outcomes_includes_check_evidence():
    outcome = ScenarioOutcome(memory_counts={"vic@example.test": 6})
    check = OutcomeCheck(
        description="Vic memory cap",
        predicate=lambda o: o.memory_counts.get("vic@example.test", 0) <= 6,
        evidence=lambda o: {"count": o.memory_counts.get("vic@example.test", 0)},
    )

    score = score_scenario_outcomes(
        outcome, [check], real_process=False, llm_personas=False
    )

    assert score.passed is True
    assert score.findings[0].evidence == {"count": 6}
