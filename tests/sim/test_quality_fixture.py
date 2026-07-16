"""End-to-end quality-scoring fixture modeled on run 20260710T040927Z.

The "legacy" fixture reproduces the observable failure modes of that run before
the five-task stack landed: replies routed to the wrong recipient, repeated
no-op admin alerts, a consent-request burst to one persona, a proposal for a
configured weak-match pair, a bundled-token consent reply, and a Petra
provenance expectation that only Elise's memory can satisfy. The "stacked"
fixture models the same population after the stack: sender-bound replies, no
no-op alerts, paced proposals, thread-faithful consent replies, and a
persona-bound Petra memory. The legacy fixture must fail every relevant check
and the stacked fixture must pass all of them.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from thenetwork.db.models import Memory
from thenetwork.sim.run.mail import SimMessageMeta, SimPostOffice
from thenetwork.sim.scoring.scoring import (
    MemoryExpectation,
    ResponseQualityThresholds,
    score_memory_expectations,
    score_response_quality,
)

AGENT = "join@example.test"
INES = "ines.sim@example.test"
VIC = "vic.sim@example.test"
DANA = "dana.sim@example.test"
PRIYA = "priya.sim@example.test"
OMAR = "omar.sim@example.test"
PETRA = "petra.sim@example.test"
ELISE = "elise.sim@example.test"
ADMIN = "admin@example.test"

_BURST_TOKENS = tuple(f"{d}0000000-0000-0000-0000-00000000000{d}" for d in "1234567")
_WEAK_PAIR_TOKEN = "77777777-7777-7777-7777-777777777777"
_VIC_TOKEN_A = "88888888-8888-8888-8888-888888888888"
_VIC_TOKEN_B = "99999999-9999-9999-9999-999999999999"
_REVEAL_PAIR_TOKEN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

_WEAK_THRESHOLDS = ResponseQualityThresholds(
    weak_match_pairs=(frozenset({DANA, OMAR}),)
)

_PETRA_EXPECTATION = MemoryExpectation(
    description="Petra provenance interest remembered",
    gist_contains="provenance",
    persona_email=PETRA,
)


def _persona(
    sender: str,
    body: str,
    *,
    subject: str,
    message_id: str,
    tick: int = 1,
) -> tuple[EmailMessage, SimMessageMeta]:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = AGENT
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content(body)
    meta = SimMessageMeta(
        tick=tick,
        direction="persona->agent",
        persona=sender.split("@")[0],
        trace_id=f"trace-{message_id}",
    )
    return message, meta


def _agent(
    recipients: str | tuple[str, ...],
    body: str,
    *,
    subject: str,
    in_reply_to: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = AGENT
    message["To"] = recipients
    message["Subject"] = subject
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    message.set_content(body)
    return message


def _consent_request(recipient: str, token: str) -> EmailMessage:
    return _agent(
        recipient,
        f"Someone in the network may be a match. Reply YES [intro:{token}]",
        subject=f"Possible introduction [intro:{token}]",
    )


def _build_legacy_mbox(mbox_path: Path) -> None:
    post_office = SimPostOffice(mbox_path=mbox_path)

    # Ines asks a question; the reply is delivered to Vic instead of her.
    message, meta = _persona(
        INES,
        "Why was this introduction chosen for me?",
        subject="Re: Possible introduction",
        message_id="<ines-question@sim>",
    )
    post_office.deliver(message, meta)
    post_office.deliver(
        _agent(
            VIC,
            "Here is why the introduction was chosen.",
            subject="Re: Possible introduction",
            in_reply_to="<ines-question@sim>",
        )
    )

    # Proactive scans that produced no action still alerted the admins.
    for index in range(3):
        post_office.deliver(
            _agent(
                ADMIN,
                f"The agent produced no reply for synthetic scan {index}.",
                subject="[The Network] Agent response needs review",
            )
        )

    # Ungated matching sent Ines seven consent requests in one run.
    for token in _BURST_TOKENS:
        post_office.deliver(_consent_request(INES, token))

    # A proposal for the configured weak-match pair.
    post_office.deliver(_consent_request(DANA, _WEAK_PAIR_TOKEN))
    post_office.deliver(_consent_request(OMAR, _WEAK_PAIR_TOKEN))

    # Vic answers two proposals in one reply, on the wrong thread.
    message, meta = _persona(
        VIC,
        f"YES [intro:{_VIC_TOKEN_A}]\nYES [intro:{_VIC_TOKEN_B}]",
        subject=f"Re: Possible introduction [intro:{_VIC_TOKEN_A}]",
        message_id="<vic-bundle@sim>",
        tick=2,
    )
    post_office.deliver(message, meta)


def _build_stacked_mbox(mbox_path: Path) -> None:
    post_office = SimPostOffice(mbox_path=mbox_path)

    # Ines asks the same question; the reply goes back to Ines.
    message, meta = _persona(
        INES,
        "Why was this introduction chosen for me?",
        subject="Re: Possible introduction",
        message_id="<ines-question@sim>",
    )
    post_office.deliver(message, meta)
    post_office.deliver(
        _agent(
            INES,
            "Please reply YES or NO so I can record your decision.",
            subject="Re: Possible introduction",
            in_reply_to="<ines-question@sim>",
        )
    )

    # Paced proposals: at most one open consent request per recipient.
    post_office.deliver(_consent_request(INES, _BURST_TOKENS[0]))
    post_office.deliver(_consent_request(DANA, _REVEAL_PAIR_TOKEN))
    post_office.deliver(_consent_request(PRIYA, _REVEAL_PAIR_TOKEN))

    # Vic replies thread-faithfully, one decision per proposal thread.
    message, meta = _persona(
        VIC,
        f"YES [intro:{_VIC_TOKEN_A}]",
        subject=f"Re: Possible introduction [intro:{_VIC_TOKEN_A}]",
        message_id="<vic-reply@sim>",
        tick=2,
    )
    post_office.deliver(message, meta)

    # The authorized dual-recipient reveal must not read as a misrouted reply.
    post_office.deliver(
        _agent(
            (DANA, PRIYA),
            "You both opted in, so here is your introduction.",
            subject="Your introduction",
        )
    )


def _legacy_memories() -> list[Memory]:
    return [
        Memory(
            id="mem-elise",
            text="raw",
            refs=["elise-id"],
            gist="interested in digital archives and provenance systems for museums",
        )
    ]


def _stacked_memories() -> list[Memory]:
    return _legacy_memories() + [
        Memory(
            id="mem-petra",
            text="raw",
            refs=["petra-id"],
            gist="interested in museum-archive provenance research",
        )
    ]


_EMAILS_BY_ID = {"elise-id": ELISE, "petra-id": PETRA}


def test_legacy_fixture_fails_every_quality_check(tmp_path):
    mbox_path = tmp_path / "legacy.mbox"
    _build_legacy_mbox(mbox_path)

    score = score_response_quality(mbox_path, thresholds=_WEAK_THRESHOLDS)

    assert score.passed is False
    failed_messages = [f.message for f in score.findings if not f.passed]
    assert any("someone other than its inbound sender" in m for m in failed_messages)
    assert any("admin alerts exceed the limit" in m for m in failed_messages)
    assert any("Consent-request burst" in m for m in failed_messages)
    assert any("weak-match pair" in m for m in failed_messages)
    assert any("bundled or mismatched thread tokens" in m for m in failed_messages)


def test_legacy_fixture_fails_petra_memory_expectation():
    score = score_memory_expectations(
        _legacy_memories(), [_PETRA_EXPECTATION], emails_by_id=_EMAILS_BY_ID
    )

    assert score.passed is False
    evidence = score.findings[0].evidence
    assert evidence["persona_email"] == PETRA
    assert evidence["gist_matches_other_owners"] == [
        {"memory_id": "mem-elise", "owner_emails": [ELISE]}
    ]


def test_stacked_fixture_passes_quality_and_memory_checks(tmp_path):
    mbox_path = tmp_path / "stacked.mbox"
    _build_stacked_mbox(mbox_path)

    quality = score_response_quality(mbox_path, thresholds=_WEAK_THRESHOLDS)
    memory = score_memory_expectations(
        _stacked_memories(), [_PETRA_EXPECTATION], emails_by_id=_EMAILS_BY_ID
    )

    assert quality.passed is True
    assert len(quality.findings) == 1
    assert memory.passed is True
    assert memory.findings[0].evidence == {"memory_id": "mem-petra"}
