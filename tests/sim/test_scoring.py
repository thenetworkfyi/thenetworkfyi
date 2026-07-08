from __future__ import annotations

from email.message import EmailMessage

from thenetwork.db.models import Memory
from thenetwork.sim.mail import SimMessageMeta, SimPostOffice
from thenetwork.sim.scoring import (
    MemoryExpectation,
    PersonaPII,
    build_transcript_judge,
    score_memory_expectations,
    score_seal_mbox,
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
    judge = build_transcript_judge()

    assert "over-promising" in judge.rubric
    assert "SEAL" in judge.rubric

