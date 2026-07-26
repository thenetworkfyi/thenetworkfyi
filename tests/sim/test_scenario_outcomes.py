from __future__ import annotations

from dataclasses import replace

import pytest

from thenetwork.db.models import Memory
from thenetwork.sim.personas.population import (
    CHLOE_EMAIL,
    DEFAULT_EXPECTATIONS,
    DEFAULT_OUTCOME_CHECKS,
    EVENT_ATTENDEE_EMAIL,
    EVENT_CONTROL_EMAIL,
    EVENT_ORGANIZER_EMAIL,
    FELIX_EMAIL,
    GABI_EMAIL,
    HUGO_EMAIL,
    LEILA_EMAIL,
    MATEO_EMAIL,
    NADIA_EMAIL,
    PETRA_EMAIL,
    ROSA_EMAIL,
    TARIQ_EMAIL,
)
from thenetwork.sim.scoring.scoring import (
    EventOutcomeFact,
    EventRecommendationOutcomeFact,
    IntroductionConsentState,
    MailFacts,
    OutcomeCheck,
    ProactiveEventTriggerOutcomeFact,
    ScenarioOutcome,
    score_memory_expectations,
    score_scenario_outcomes,
)


def _outcome() -> ScenarioOutcome:
    return ScenarioOutcome(
        consent_rows=(
            IntroductionConsentState(
                person_a_email="alice@example.test",
                person_b_email="bob@example.test",
                status="introduced",
            ),
        ),
        audit_events=({"event": "introduction.sent"},),
        mail_facts=(
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({"alice@example.test", "bob@example.test"}),
                subject="Your introduction",
                body="You both opted in.",
            ),
        ),
        memory_counts={"alice@example.test": 2},
    )


def test_score_scenario_outcomes_records_predicate_pass_and_fail():
    score = score_scenario_outcomes(
        _outcome(),
        (
            OutcomeCheck(
                description="an introduction was sent",
                predicate=lambda outcome: any(
                    mail.subject == "Your introduction" for mail in outcome.mail_facts
                ),
            ),
            OutcomeCheck(
                description="Alice has at most one memory",
                predicate=lambda outcome: (
                    outcome.memory_counts["alice@example.test"] <= 1
                ),
            ),
        ),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False
    assert [finding.passed for finding in score.findings] == [True, False]
    assert [finding.message for finding in score.findings] == [
        "an introduction was sent",
        "Alice has at most one memory",
    ]


def test_score_scenario_outcomes_skips_real_process_check_in_mock_mode():
    score = score_scenario_outcomes(
        _outcome(),
        (
            OutcomeCheck(
                description="audit event exists",
                predicate=lambda _outcome: False,
                requires_real_process=True,
            ),
        ),
        real_process=False,
        llm_personas=True,
    )

    assert score.passed is True
    assert score.findings[0].passed is True
    assert score.findings[0].evidence == {"skipped": True}
    assert score.findings[0].message == (
        "audit event exists (skipped: real-process mode is disabled)"
    )


def test_score_scenario_outcomes_skips_llm_check_without_llm_personas():
    score = score_scenario_outcomes(
        _outcome(),
        (
            OutcomeCheck(
                description="persona declines naturally",
                predicate=lambda _outcome: False,
                requires_llm_personas=True,
            ),
        ),
        real_process=True,
        llm_personas=False,
    )

    assert score.passed is True
    assert score.findings[0].message == (
        "persona declines naturally (skipped: LLM-persona mode is disabled)"
    )


def test_score_scenario_outcomes_reports_all_reasons_when_both_modes_required():
    score = score_scenario_outcomes(
        _outcome(),
        (
            OutcomeCheck(
                description="real persona interaction is audited",
                predicate=lambda _outcome: False,
                requires_real_process=True,
                requires_llm_personas=True,
            ),
        ),
        real_process=False,
        llm_personas=False,
    )

    assert score.findings[0].message == (
        "real persona interaction is audited (skipped: real-process mode is disabled; "
        "LLM-persona mode is disabled)"
    )


def test_score_scenario_outcomes_empty_checks_are_a_deterministic_pass():
    score = score_scenario_outcomes(
        _outcome(),
        (),
        real_process=False,
        llm_personas=False,
    )

    assert score.passed is True
    assert len(score.findings) == 1
    assert score.findings[0].message == "No scenario outcome checks configured"


def _default_outcome() -> ScenarioOutcome:
    return ScenarioOutcome(
        consent_rows=(
            IntroductionConsentState(
                person_a_email="ruth.sim@example.test",
                person_b_email="peer@example.test",
                status="declined",
            ),
            IntroductionConsentState(
                person_a_email="omar.sim@example.test",
                person_b_email="waiting@example.test",
                status="one_consented",
            ),
            *(
                IntroductionConsentState(
                    person_a_email="vic.sim@example.test",
                    person_b_email=f"vic-peer-{index}@example.test",
                    status="proposed",
                )
                for index in range(6)
            ),
            IntroductionConsentState(
                person_a_email=LEILA_EMAIL,
                person_b_email=MATEO_EMAIL,
                status="proposed",
            ),
        ),
        audit_events=(
            {
                "event": "introduction.consent_transition",
                "action": "clarify",
                "outcome": "success",
                "sender_id_hash": "snd_v1_ines",
            },
            {
                "event": "introduction.consent_transition",
                "action": "consent",
                "outcome": "success",
                "consent_state": "one_consented",
                "sender_id_hash": "snd_v1_omar",
            },
            {
                "event": "agent.tool.completed",
                "tool_name": "send_event_recommendation",
                "outcome": "success",
                "sender_id_hash": "snd_v1_mina",
            },
            *(
                {
                    "event": "agent.tool.completed",
                    "tool_name": tool_name,
                    "outcome": "success",
                    "sender_id_hash": "snd_v1_leila",
                }
                for tool_name in (
                    "remember",
                    "remember",
                    "forget",
                    "forget",
                    "remember",
                    "remember",
                    "forget",
                    "forget",
                    "remember",
                    "propose_introduction",
                )
            ),
        ),
        sender_id_hashes={
            "omar.sim@example.test": "snd_v1_omar",
            "ines.sim@example.test": "snd_v1_ines",
            EVENT_ORGANIZER_EMAIL: "snd_v1_sloane",
            EVENT_ATTENDEE_EMAIL: "snd_v1_mina",
            EVENT_CONTROL_EMAIL: "snd_v1_theo",
            LEILA_EMAIL: "snd_v1_leila",
        },
        mail_facts=(
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({"ines.sim@example.test"}),
                subject="Re: Possible introduction",
                body=(
                    "I could not determine your response. Reply with YES to opt in, "
                    "NO to decline, or REVOKE to withdraw consent."
                ),
            ),
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({EVENT_ATTENDEE_EMAIL}),
                subject="An event you might care about",
                body=(
                    "An event that may be relevant. If you don't want more event "
                    "recommendations, reply no to opt out."
                ),
            ),
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({HUGO_EMAIL}),
                subject="Re: The Network",
                body="What kind of work and counterpart are you looking for?",
            ),
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({TARIQ_EMAIL}),
                subject="Re: The Network",
                body="What type of climate work are you focused on?",
            ),
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({LEILA_EMAIL}),
                subject="Re: The Network",
                body="What role and relevant experience do you bring to this lab tool?",
            ),
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({LEILA_EMAIL}),
                subject="Re: The Network",
                body="What kind of peer exchange and working rhythm would be useful?",
            ),
            MailFacts(
                sender=LEILA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="Lab tools",
                body=(
                    "I am building inventory software for community science labs and "
                    "would like to meet someone else working on lab tools."
                ),
            ),
            MailFacts(
                sender=ROSA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="Hello from Oakland",
                body=(
                    "I have been a data engineer for eight years, though that is just "
                    "what pays the rent. I have done Lindy Hop for six years and I "
                    "play upright bass in a small swing band."
                ),
            ),
            MailFacts(
                sender="join@example.test",
                recipients=frozenset({ROSA_EMAIL}),
                subject="Re: Hello from Oakland",
                body="What is the band short of for dance gigs?",
            ),
            MailFacts(
                sender=LEILA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="Re: Lab tools",
                body=(
                    "I am the product designer and have piloted the tool with two "
                    "volunteer-run community science labs."
                ),
            ),
            MailFacts(
                sender=LEILA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="Re: Lab tools",
                body=(
                    "I want a peer product designer with hands-on workflow-adoption "
                    "experience. I can meet remotely every other week for three months."
                ),
            ),
        ),
        memory_counts={"vic.sim@example.test": 6, LEILA_EMAIL: 1},
        event_rows=(
            EventOutcomeFact(
                event_key="evt_v1_default",
                owner_sender_id_hash="snd_v1_sloane",
                version=1,
                active=True,
                recurring=True,
            ),
        ),
        event_recommendation_rows=(
            EventRecommendationOutcomeFact(
                event_key="evt_v1_default",
                recipient_sender_id_hash="snd_v1_mina",
                event_version=1,
                notified=True,
            ),
        ),
        proactive_event_triggers=(
            ProactiveEventTriggerOutcomeFact(
                event_key="evt_v1_default",
                recipient_sender_id_hash="snd_v1_mina",
                event_version=1,
            ),
        ),
    )


def _with_leila_tool_sequence(
    outcome: ScenarioOutcome, tool_names: tuple[str, ...]
) -> ScenarioOutcome:
    sender_id_hash = outcome.sender_id_hashes[LEILA_EMAIL]
    other_events = tuple(
        event
        for event in outcome.audit_events
        if event.get("sender_id_hash") != sender_id_hash
    )
    leila_events = tuple(
        {
            "event": "agent.tool.completed",
            "tool_name": tool_name,
            "outcome": "success",
            "sender_id_hash": sender_id_hash,
        }
        for tool_name in tool_names
    )
    return replace(outcome, audit_events=(*other_events, *leila_events))


def test_default_outcome_checks_cover_all_persona_situations():
    score = score_scenario_outcomes(
        _default_outcome(),
        DEFAULT_OUTCOME_CHECKS,
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True
    assert len(score.findings) == 23
    assert all(check.requires_real_process for check in DEFAULT_OUTCOME_CHECKS)
    assert all(check.requires_llm_personas for check in DEFAULT_OUTCOME_CHECKS)


@pytest.mark.parametrize(
    ("check_index", "outcome"),
    [
        (
            0,
            replace(
                _default_outcome(),
                consent_rows=(
                    IntroductionConsentState(
                        person_a_email="ruth.sim@example.test",
                        person_b_email="peer@example.test",
                        status="proposed",
                    ),
                ),
            ),
        ),
        (
            1,
            replace(
                _default_outcome(),
                mail_facts=(
                    MailFacts(
                        sender="join@example.test",
                        recipients=frozenset(
                            {"ruth.sim@example.test", "peer@example.test"}
                        ),
                        subject="Your introduction",
                        body="You both opted in.",
                    ),
                ),
            ),
        ),
        (2, replace(_default_outcome(), audit_events=())),
        (
            3,
            replace(
                _default_outcome(),
                mail_facts=(
                    MailFacts(
                        sender="join@example.test",
                        recipients=frozenset({"ines.sim@example.test"}),
                        subject="Re: Possible introduction",
                        body="Tell me more about this person.",
                    ),
                ),
            ),
        ),
        (
            4,
            replace(_default_outcome(), memory_counts={"vic.sim@example.test": 7}),
        ),
        (
            5,
            replace(
                _default_outcome(),
                consent_rows=tuple(
                    IntroductionConsentState(
                        person_a_email="vic.sim@example.test",
                        person_b_email=f"vic-peer-{index}@example.test",
                        status="proposed",
                    )
                    for index in range(7)
                ),
            ),
        ),
        (
            6,
            replace(
                _default_outcome(),
                consent_rows=(
                    IntroductionConsentState(
                        person_a_email="ruth.sim@example.test",
                        person_b_email="peer@example.test",
                        status="declined",
                    ),
                    *(
                        IntroductionConsentState(
                            person_a_email="vic.sim@example.test",
                            person_b_email=f"vic-peer-{index}@example.test",
                            status="proposed",
                        )
                        for index in range(6)
                    ),
                ),
            ),
        ),
        (
            7,
            replace(
                _default_outcome(),
                audit_events=(),
            ),
        ),
        (
            8,
            replace(
                _default_outcome(),
                mail_facts=(
                    MailFacts(
                        sender="join@example.test",
                        recipients=frozenset(
                            {"omar.sim@example.test", "peer@example.test"}
                        ),
                        subject="Your introduction",
                        body="You both opted in.",
                    ),
                ),
            ),
        ),
    ],
)
def test_default_outcome_checks_have_failure_fixtures(
    check_index: int,
    outcome: ScenarioOutcome,
):
    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[check_index],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False
    assert score.findings[0].passed is False


@pytest.mark.parametrize(
    ("check_index", "outcome"),
    [
        (9, replace(_default_outcome(), event_rows=())),
        (10, replace(_default_outcome(), proactive_event_triggers=())),
        (
            11,
            replace(
                _default_outcome(),
                event_recommendation_rows=(
                    EventRecommendationOutcomeFact(
                        event_key="evt_v1_default",
                        recipient_sender_id_hash="snd_v1_mina",
                        event_version=1,
                        notified=False,
                    ),
                ),
            ),
        ),
        (
            12,
            replace(
                _default_outcome(),
                event_recommendation_rows=(
                    *_default_outcome().event_recommendation_rows,
                    EventRecommendationOutcomeFact(
                        event_key="evt_v1_default",
                        recipient_sender_id_hash="snd_v1_sloane",
                        event_version=1,
                        notified=False,
                    ),
                ),
            ),
        ),
    ],
)
def test_event_outcome_checks_have_failure_fixtures(
    check_index: int,
    outcome: ScenarioOutcome,
):
    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[check_index],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False


def test_event_outcome_evidence_contains_only_safe_correlation_facts():
    score = score_scenario_outcomes(
        _default_outcome(),
        DEFAULT_OUTCOME_CHECKS[9:13],
        real_process=True,
        llm_personas=True,
    )

    evidence = repr([finding.evidence for finding in score.findings])
    assert score.passed is True
    for sensitive in (
        EVENT_ORGANIZER_EMAIL,
        EVENT_ATTENDEE_EMAIL,
        EVENT_CONTROL_EMAIL,
        "municipal-library facilities teams",
        "Sloane Park",
    ):
        assert sensitive not in evidence
    assert "evt_v1_default" in evidence


@pytest.mark.parametrize(
    ("check_index", "outcome"),
    [
        (13, replace(_default_outcome(), memory_counts={FELIX_EMAIL: 1})),
        (14, replace(_default_outcome(), memory_counts={GABI_EMAIL: 1})),
        (
            15,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    message
                    for message in _default_outcome().mail_facts
                    if HUGO_EMAIL not in message.recipients
                ),
            ),
        ),
        (
            16,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    message
                    for message in _default_outcome().mail_facts
                    if TARIQ_EMAIL not in message.recipients
                ),
            ),
        ),
        (17, replace(_default_outcome(), memory_counts={CHLOE_EMAIL: 1})),
    ],
)
def test_tofu_outcome_checks_have_failure_fixtures(
    check_index: int,
    outcome: ScenarioOutcome,
):
    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[check_index],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False
    assert score.findings[0].passed is False


def test_rosa_checks_are_unexercised_when_she_never_states_both_pursuits():
    """A run where Rosa never sends her opening must not report a product failure.

    Both predicates are guarded on her having actually stated the dance and
    music threads, so an offline or budget-truncated run records the situation
    as unexercised rather than passing off a silent no-op as correct behavior.
    """
    outcome = replace(
        _default_outcome(),
        mail_facts=tuple(
            message
            for message in _default_outcome().mail_facts
            if ROSA_EMAIL not in message.recipients and message.sender != ROSA_EMAIL
        ),
        consent_rows=(
            *_default_outcome().consent_rows,
            IntroductionConsentState(
                person_a_email=ROSA_EMAIL,
                person_b_email="priya.sim@example.test",
                status="proposed",
            ),
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        DEFAULT_OUTCOME_CHECKS[21:23],
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True
    assert all(
        finding.evidence["pursuits_stated"] is False for finding in score.findings
    )


def test_rosa_evidence_does_not_expose_mail_content():
    """Public evidence carries bounded counts and markers, never body text."""
    private_body = "Private sender-authored detail about a job at Initech"
    outcome = replace(
        _default_outcome(),
        mail_facts=tuple(
            replace(message, body=private_body)
            if ROSA_EMAIL in message.recipients
            else message
            for message in _default_outcome().mail_facts
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        DEFAULT_OUTCOME_CHECKS[21:23],
        real_process=True,
        llm_personas=True,
    )

    assert private_body not in repr(score.findings)
    assert "Initech" not in repr(score.findings)


def test_tofu_scope_question_evidence_does_not_expose_mail_content():
    private_subject = "Private sender-authored project details"
    outcome = replace(
        _default_outcome(),
        mail_facts=tuple(
            replace(message, subject=private_subject)
            if HUGO_EMAIL in message.recipients or TARIQ_EMAIL in message.recipients
            else message
            for message in _default_outcome().mail_facts
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        DEFAULT_OUTCOME_CHECKS[15:17],
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True
    assert all(finding.evidence == {"question_count": 1} for finding in score.findings)
    assert private_subject not in repr(score.findings)


@pytest.mark.parametrize(
    ("check_index", "outcome"),
    [
        (
            18,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    message
                    for message in _default_outcome().mail_facts
                    if not (
                        LEILA_EMAIL in message.recipients
                        and "working rhythm" in message.body
                    )
                ),
            ),
        ),
        (
            18,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    replace(message, body="Can you say more?")
                    if LEILA_EMAIL in message.recipients
                    else message
                    for message in _default_outcome().mail_facts
                ),
            ),
        ),
        (19, replace(_default_outcome(), memory_counts={LEILA_EMAIL: 2})),
        (
            19,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    message
                    for message in _default_outcome().mail_facts
                    if not (
                        message.sender == LEILA_EMAIL
                        and "product designer" in message.body
                    )
                ),
            ),
        ),
        (
            19,
            _with_leila_tool_sequence(
                _default_outcome(),
                ("remember", "remember", "forget", "forget", "remember"),
            ),
        ),
        (
            20,
            _with_leila_tool_sequence(
                _default_outcome(),
                (
                    "remember",
                    "remember",
                    "forget",
                    "forget",
                    "remember",
                    "propose_introduction",
                ),
            ),
        ),
        (
            20,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    message
                    for message in _default_outcome().mail_facts
                    if not (
                        message.sender == LEILA_EMAIL
                        and "peer product designer" in message.body
                    )
                ),
            ),
        ),
        (
            20,
            replace(
                _default_outcome(),
                consent_rows=tuple(
                    row
                    for row in _default_outcome().consent_rows
                    if LEILA_EMAIL not in row.participant_emails
                )
                + (
                    IntroductionConsentState(
                        person_a_email=LEILA_EMAIL,
                        person_b_email="unrelated.sim@example.test",
                        status="proposed",
                    ),
                ),
            ),
        ),
        (
            20,
            replace(
                _default_outcome(),
                audit_events=(
                    {
                        "event": "agent.tool.completed",
                        "tool_name": "propose_introduction",
                        "outcome": "success",
                        "sender_id_hash": "snd_v1_leila",
                    },
                    *_default_outcome().audit_events,
                ),
            ),
        ),
        # Rosa named a day job but never asked for work; qualifying her as a
        # job seeker is the multi-register failure this situation exists for.
        (
            21,
            replace(
                _default_outcome(),
                mail_facts=tuple(
                    replace(
                        message,
                        body="What kind of role are you looking for a job in?",
                    )
                    if ROSA_EMAIL in message.recipients
                    else message
                    for message in _default_outcome().mail_facts
                ),
            ),
        ),
        # An introduction driven by her data-engineering keywords rather than
        # the dance-and-music ask she actually made.
        (
            22,
            replace(
                _default_outcome(),
                consent_rows=(
                    *_default_outcome().consent_rows,
                    IntroductionConsentState(
                        person_a_email=ROSA_EMAIL,
                        person_b_email="priya.sim@example.test",
                        status="proposed",
                    ),
                ),
            ),
        ),
    ],
)
def test_match_depth_outcome_checks_have_failure_fixtures(
    check_index: int,
    outcome: ScenarioOutcome,
):
    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[check_index],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False
    assert score.findings[0].passed is False


def test_match_depth_outcome_evidence_is_privacy_safe():
    score = score_scenario_outcomes(
        _default_outcome(),
        DEFAULT_OUTCOME_CHECKS[18:],
        real_process=True,
        llm_personas=True,
    )

    evidence = repr([finding.evidence for finding in score.findings])
    assert score.passed is True
    for private_value in (
        LEILA_EMAIL,
        MATEO_EMAIL,
        "Leila Hart",
        "community science labs",
        "working rhythm",
    ):
        assert private_value not in evidence


def test_ines_clarification_check_ignores_other_personas_clarify_events():
    outcome = replace(
        _default_outcome(),
        audit_events=(
            {
                "event": "introduction.consent_transition",
                "action": "clarify",
                "outcome": "success",
                "sender_id_hash": "snd_v1_vic",
            },
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[2],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False
    assert score.findings[0].evidence == {"clarify_events": []}


def test_ines_clarification_check_ignores_unscoped_clarify_events():
    outcome = replace(
        _default_outcome(),
        audit_events=(
            {
                "event": "introduction.consent_transition",
                "action": "clarify",
                "outcome": "success",
            },
        ),
        sender_id_hashes={"omar.sim@example.test": "snd_v1_omar"},
    )

    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[2],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False
    assert score.findings[0].evidence == {"clarify_events": []}


def test_ines_clarification_check_passes_for_her_own_clarify_event():
    score = score_scenario_outcomes(
        _default_outcome(),
        (DEFAULT_OUTCOME_CHECKS[2],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True
    assert score.findings[0].evidence == {
        "clarify_events": [
            {
                "event": "introduction.consent_transition",
                "action": "clarify",
                "outcome": "success",
                "sender_id_hash": "snd_v1_ines",
            }
        ]
    }


def test_omar_outcome_uses_his_audited_action_not_final_pair_status():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionConsentState(
                person_a_email="omar.sim@example.test",
                person_b_email="samir.sim@example.test",
                status="revoked",
            ),
            IntroductionConsentState(
                person_a_email="omar.sim@example.test",
                person_b_email="ines.sim@example.test",
                status="proposed",
            ),
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[7],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True
    assert score.findings[0].evidence["consent_events"] == [
        {
            "event": "introduction.consent_transition",
            "action": "consent",
            "outcome": "success",
            "consent_state": "one_consented",
            "sender_id_hash": "snd_v1_omar",
        }
    ]


def test_omar_outcome_accepts_counterpart_first_consent_and_mutual_handoff():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionConsentState(
                person_a_email="omar.sim@example.test",
                person_b_email="samir.sim@example.test",
                status="introduced",
            ),
        ),
        audit_events=(
            {
                "event": "introduction.consent_transition",
                "action": "consent",
                "outcome": "success",
                "consent_state": "introduced",
                "sender_id_hash": "snd_v1_omar",
            },
        ),
        mail_facts=(
            MailFacts(
                sender="join@example.test",
                recipients=frozenset(
                    {"omar.sim@example.test", "samir.sim@example.test"}
                ),
                subject="Your introduction",
                body="You both opted in.",
            ),
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        DEFAULT_OUTCOME_CHECKS[7:9],
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True


def test_omar_handoff_accepts_pair_revoked_after_mutual_consent():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionConsentState(
                person_a_email="omar.sim@example.test",
                person_b_email="samir.sim@example.test",
                status="revoked",
                person_a_consented=True,
                person_b_consented=True,
            ),
        ),
        mail_facts=(
            MailFacts(
                sender="join@example.test",
                recipients=frozenset(
                    {"omar.sim@example.test", "samir.sim@example.test"}
                ),
                subject="Your introduction",
                body="You both opted in.",
            ),
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[8],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True


def test_omar_handoff_rejects_revoked_pair_without_mutual_consent():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionConsentState(
                person_a_email="omar.sim@example.test",
                person_b_email="samir.sim@example.test",
                status="revoked",
                person_a_consented=True,
            ),
        ),
        mail_facts=(
            MailFacts(
                sender="join@example.test",
                recipients=frozenset(
                    {"omar.sim@example.test", "samir.sim@example.test"}
                ),
                subject="Your introduction",
                body="You both opted in.",
            ),
        ),
    )

    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[8],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False


@pytest.mark.parametrize(
    "actions",
    [
        (),
        (
            {
                "event": "introduction.consent_transition",
                "action": "consent",
                "outcome": "success",
                "consent_state": "one_consented",
                "sender_id_hash": "snd_v1_omar",
            },
            {
                "event": "introduction.consent_transition",
                "action": "consent",
                "outcome": "success",
                "consent_state": "introduced",
                "sender_id_hash": "snd_v1_omar",
            },
        ),
        (
            {
                "event": "introduction.consent_transition",
                "action": "revoke",
                "outcome": "success",
                "consent_state": "revoked",
                "sender_id_hash": "snd_v1_omar",
            },
        ),
    ],
)
def test_omar_outcome_rejects_missing_repeated_or_revoked_consent(actions):
    outcome = replace(_default_outcome(), audit_events=actions)

    score = score_scenario_outcomes(
        outcome,
        (DEFAULT_OUTCOME_CHECKS[7],),
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is False


@pytest.mark.parametrize(
    ("expectation_index", "owner_email", "gist", "expected"),
    [
        (0, NADIA_EMAIL, "Nadia is building a bakery supply co-op.", True),
        (0, NADIA_EMAIL, "Nadia is still in ML infrastructure.", False),
        (1, PETRA_EMAIL, "Petra studies provenance for museum archives.", True),
        (1, PETRA_EMAIL, "Petra wants generic networking advice.", False),
        (2, HUGO_EMAIL, "Hugo operates patient-scheduling systems.", True),
        (2, HUGO_EMAIL, "Hugo wants generic networking advice.", False),
        (3, TARIQ_EMAIL, "Tariq runs public-school heat-pump retrofits.", True),
        (3, TARIQ_EMAIL, "Tariq wants generic climate contacts.", False),
        (4, LEILA_EMAIL, "Leila is a product designer for a community lab.", True),
        (4, LEILA_EMAIL, "Leila wants generic lab contacts.", False),
        (5, LEILA_EMAIL, "Leila wants a remote peer exchange.", True),
        (5, LEILA_EMAIL, "Leila wants an in-person hiring lead.", False),
    ],
)
def test_default_memory_expectations_have_pass_and_fail_fixtures(
    expectation_index: int,
    owner_email: str,
    gist: str,
    expected: bool,
):
    expectation = DEFAULT_EXPECTATIONS[expectation_index]
    if expectation.inbound_required_groups:
        inbound_body = " ".join(
            group[0] for group in expectation.inbound_required_groups
        )
    else:
        inbound_body = expectation.inbound_contains_any[0]
    score = score_memory_expectations(
        (Memory(id="memory-1", text="raw", refs=[owner_email], gist=gist),),
        (expectation,),
        mail_facts=(
            MailFacts(
                sender=owner_email,
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body=inbound_body,
            ),
        ),
    )

    assert score.passed is expected


def test_match_depth_memory_expectations_are_unexercised_without_persona_mail():
    score = score_memory_expectations((), DEFAULT_EXPECTATIONS[4:], mail_facts=())

    assert score.passed is True
    assert all(
        finding.evidence == {"unexercised": True, "persona_inbound_messages_checked": 0}
        for finding in score.findings
    )


def test_match_depth_memory_expectation_waits_for_every_prerequisite_group():
    expectation = DEFAULT_EXPECTATIONS[4]
    opening_only = MailFacts(
        sender=LEILA_EMAIL,
        recipients=frozenset({"join@example.test"}),
        subject="Lab tools",
        body="I am building inventory software for community science labs.",
    )

    unexercised = score_memory_expectations(
        (), (expectation,), mail_facts=(opening_only,)
    )
    exercised = score_memory_expectations(
        (),
        (expectation,),
        mail_facts=(
            opening_only,
            MailFacts(
                sender=LEILA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="Re: Lab tools",
                body="I have piloted it with two volunteer-run community science labs.",
            ),
        ),
    )

    assert unexercised.passed is True
    assert unexercised.findings[0].evidence["unexercised"] is True
    assert exercised.passed is False
    assert exercised.findings[0].evidence == {}


def test_hugo_scope_memory_waits_for_the_clarifying_reply():
    expectation = DEFAULT_EXPECTATIONS[2]
    vague_opening = MailFacts(
        sender=HUGO_EMAIL,
        recipients=frozenset({"join@example.test"}),
        subject="Clinic systems",
        body="I want an introduction to someone working with community health clinics.",
    )

    unexercised = score_memory_expectations(
        (), (expectation,), mail_facts=(vague_opening,)
    )
    exercised = score_memory_expectations(
        (),
        (expectation,),
        mail_facts=(
            vague_opening,
            MailFacts(
                sender=HUGO_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="Re: Clinic systems",
                body="The scope is patient-scheduling systems.",
            ),
        ),
    )

    assert unexercised.passed is True
    assert unexercised.findings[0].evidence["unexercised"] is True
    assert exercised.passed is False
    assert exercised.findings[0].evidence == {}


def test_default_memory_expectations_reject_wrong_persona_owner():
    score = score_memory_expectations(
        (
            Memory(
                id="memory-1",
                text="raw",
                refs=["elise-id"],
                gist="Elise studies provenance for museum archives.",
            ),
        ),
        (DEFAULT_EXPECTATIONS[1],),
        emails_by_id={"elise-id": "elise.sim@example.test"},
        mail_facts=(
            MailFacts(
                sender=PETRA_EMAIL,
                recipients=frozenset({"join@example.test"}),
                subject="A note",
                body="I study provenance for museum archives.",
            ),
        ),
    )

    assert score.passed is False
    evidence = score.findings[0].evidence
    assert evidence["persona_email"] == PETRA_EMAIL
    assert evidence["gist_matches_other_owners"] == [
        {"memory_id": "memory-1", "owner_emails": ["elise.sim@example.test"]}
    ]
