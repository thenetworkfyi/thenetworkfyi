from __future__ import annotations

from dataclasses import replace

import pytest

from thenetwork.db.models import Memory
from thenetwork.sim.personas.population import (
    DEFAULT_EXPECTATIONS,
    DEFAULT_OUTCOME_CHECKS,
    NADIA_EMAIL,
    PETRA_EMAIL,
)
from thenetwork.sim.scoring.scoring import (
    IntroductionRevealAuthorization,
    MailFacts,
    OutcomeCheck,
    ScenarioOutcome,
    score_memory_expectations,
    score_scenario_outcomes,
)


def _outcome() -> ScenarioOutcome:
    return ScenarioOutcome(
        consent_rows=(
            IntroductionRevealAuthorization(
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
            IntroductionRevealAuthorization(
                person_a_email="ruth.sim@example.test",
                person_b_email="peer@example.test",
                status="declined",
            ),
            IntroductionRevealAuthorization(
                person_a_email="omar.sim@example.test",
                person_b_email="waiting@example.test",
                status="one_consented",
            ),
            *(
                IntroductionRevealAuthorization(
                    person_a_email="vic.sim@example.test",
                    person_b_email=f"vic-peer-{index}@example.test",
                    status="proposed",
                )
                for index in range(6)
            ),
        ),
        audit_events=(
            {
                "event": "introduction.consent_transition",
                "action": "clarify",
                "outcome": "success",
            },
            {
                "event": "introduction.consent_transition",
                "action": "consent",
                "outcome": "success",
                "consent_state": "one_consented",
                "sender_id_hash": "snd_v1_omar",
            },
        ),
        sender_id_hashes={"omar.sim@example.test": "snd_v1_omar"},
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
        ),
        memory_counts={"vic.sim@example.test": 6},
    )


def test_default_outcome_checks_cover_all_persona_situations():
    score = score_scenario_outcomes(
        _default_outcome(),
        DEFAULT_OUTCOME_CHECKS,
        real_process=True,
        llm_personas=True,
    )

    assert score.passed is True
    assert len(score.findings) == 9
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
                    IntroductionRevealAuthorization(
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
                    IntroductionRevealAuthorization(
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
                    IntroductionRevealAuthorization(
                        person_a_email="ruth.sim@example.test",
                        person_b_email="peer@example.test",
                        status="declined",
                    ),
                    *(
                        IntroductionRevealAuthorization(
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


def test_omar_outcome_uses_his_audited_action_not_final_pair_status():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionRevealAuthorization(
                person_a_email="omar.sim@example.test",
                person_b_email="samir.sim@example.test",
                status="revoked",
            ),
            IntroductionRevealAuthorization(
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


def test_omar_outcome_accepts_counterpart_first_consent_and_mutual_reveal():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionRevealAuthorization(
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


def test_omar_reveal_accepts_pair_revoked_after_mutual_consent():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionRevealAuthorization(
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


def test_omar_reveal_rejects_revoked_pair_without_mutual_consent():
    outcome = replace(
        _default_outcome(),
        consent_rows=(
            IntroductionRevealAuthorization(
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
    ],
)
def test_default_memory_expectations_have_pass_and_fail_fixtures(
    expectation_index: int,
    owner_email: str,
    gist: str,
    expected: bool,
):
    score = score_memory_expectations(
        (Memory(id="memory-1", text="raw", refs=[owner_email], gist=gist),),
        (DEFAULT_EXPECTATIONS[expectation_index],),
    )

    assert score.passed is expected


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
    )

    assert score.passed is False
    evidence = score.findings[0].evidence
    assert evidence["persona_email"] == PETRA_EMAIL
    assert evidence["gist_matches_other_owners"] == [
        {"memory_id": "memory-1", "owner_emails": ["elise.sim@example.test"]}
    ]
