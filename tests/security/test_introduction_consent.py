import re
from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from unittest.mock import AsyncMock, patch

import pytest

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.introductions import (
    ConsentReplyResult,
    _reply_action,
    process_consent_reply,
    propose_pair,
    _utcnow,
)
from thenetwork.email.render import ConsentRequestEmailContext, FixedEmailTemplate


@pytest.mark.integration
def test_metadata_schema_cascades_consent_when_participant_is_deleted(pg_engine):
    """SQLModel metadata must match migration 007's two cascade constraints."""
    from sqlmodel import Session

    first = Person(name="Cascade A", email="cascade-a@example.com")
    second = Person(name="Cascade B", email="cascade-b@example.com")
    with Session(pg_engine) as session:
        session.add(first)
        session.add(second)
        session.commit()
        consent = IntroductionConsent(person_a_id=first.id, person_b_id=second.id)
        session.add(consent)
        session.commit()
        consent_id = consent.id
        session.delete(first)
        session.commit()
        assert session.get(IntroductionConsent, consent_id) is None
        session.delete(second)
        session.commit()


class Result:
    def __init__(self, value, values=None):
        self.value = value
        self.values = values if values is not None else []

    def first(self):
        return self.value

    def all(self):
        return self.values


class FakeSession:
    def __init__(
        self,
        proposal=None,
        people=None,
        outstanding_proposals=None,
        recent_proposals=None,
    ):
        self.proposal = proposal
        self.people = people or {}
        self.added = []
        self.commits = 0
        self.outstanding_proposals = outstanding_proposals or []
        self.recent_proposals = recent_proposals or []

    def exec(self, query):
        rendered = str(query.compile(compile_kwargs={"literal_binds": True}))
        match = re.search(r"person_a_id = '([^']+)'", rendered)
        person_id = match.group(1) if match else None
        if "status IN" in rendered:
            rows = [
                r
                for r in self.outstanding_proposals
                if person_id in (r.person_a_id, r.person_b_id)
            ]
            return Result(None, rows)
        if "created_at >=" in rendered:
            rows = [
                r
                for r in self.recent_proposals
                if person_id in (r.person_a_id, r.person_b_id)
            ]
            return Result(None, rows)
        if " OR " in rendered:
            history = list(self.recent_proposals) + list(self.outstanding_proposals)
            if self.proposal is not None:
                history.append(self.proposal)
            rows = [r for r in history if person_id in (r.person_a_id, r.person_b_id)]
            return Result(rows[0] if rows else None, rows)
        return Result(self.proposal, self.outstanding_proposals)

    def get(self, _model, person_id):
        return self.people.get(person_id)

    def add(self, value):
        self.added.append(value)
        if isinstance(value, IntroductionConsent):
            self.proposal = value

    def commit(self):
        self.commits += 1

    def refresh(self, _value):
        return None


def factory(session):
    @contextmanager
    def open_session():
        yield session

    return open_session


def proposal(**overrides):
    values = {
        "person_a_id": "alice",
        "person_b_id": "bob",
        "reply_token": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }
    values.update(overrides)
    return IntroductionConsent(**values)


def people():
    return {
        "alice": Person(id="alice", name="Alice", email="alice@example.com"),
        "bob": Person(id="bob", name="Bob", email="bob@example.com"),
    }


def test_model_assertion_cannot_create_unauthenticated_consent():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_proxy_introduction") as group_send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=False,
            subject="Re: Possible introduction [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
            session_factory=factory(session),
        )

    assert result.handled
    assert result.outcome == "rejected"
    assert not session.proposal.person_a_consented
    group_send.assert_not_called()


@pytest.mark.parametrize("body", ["Yes.", "yes, please", "YES!"])
def test_tolerant_yes_reply_consents_without_revealing_identity(body):
    session = FakeSession(proposal=proposal(), people=people())

    with (
        patch("thenetwork.introductions.send_proxy_introduction") as group_send,
        patch("thenetwork.introductions.send_reply") as send,
    ):
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body=body,
            session_factory=factory(session),
        )

    assert result.outcome == "one_consented"
    assert session.proposal.person_a_consented
    assert not session.proposal.person_b_consented
    group_send.assert_not_called()
    send.assert_called_once()
    assert (
        send.call_args.kwargs["fixed_template"]
        is FixedEmailTemplate.CONSENT_ACKNOWLEDGMENT
    )
    assert [memory.recipient_person_id for memory in result.sent_email_memories] == [
        "alice"
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("YES", "consent"),
        ("NO", "decline"),
        ("REVOKE", "revoke"),
        ("Yes, please", "consent"),
        ("no thanks.", "decline"),
    ],
)
def test_reply_action_accepts_only_short_decisions(body, expected):
    assert _reply_action(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "I have had a change in direction. I am no longer looking for simulation modeling peers.",
        "No problem, I will reply when I have decided.",
        "Yes and I have an update about bakery supplies.",
        "Sounds good, yes",
    ],
)
def test_reply_action_rejects_prose_containing_decision_words(body):
    assert _reply_action(body) is None


def test_tokened_prose_reply_gets_clarification_without_revoking_pair():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_reply") as send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body=(
                "I have had a change in direction. "
                "I am no longer looking for simulation modeling peers."
            ),
            session_factory=factory(session),
        )

    assert result.outcome == "clarification_sent"
    assert session.proposal.status == "proposed"
    assert not session.proposal.person_a_consented
    assert session.commits == 0
    assert (
        send.call_args.kwargs["fixed_template"]
        is FixedEmailTemplate.CONSENT_CLARIFICATION
    )
    assert [memory.recipient_person_id for memory in result.sent_email_memories] == [
        "alice"
    ]


def test_punctuated_no_reply_creates_temporary_decline():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_reply") as send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="no thanks.",
            session_factory=factory(session),
        )

    assert result.outcome == "declined"
    assert session.proposal.status == "declined"
    assert session.proposal.declined_at is not None
    assert (
        send.call_args.kwargs["fixed_template"] is FixedEmailTemplate.CONSENT_DECLINED
    )
    assert [memory.recipient_person_id for memory in result.sent_email_memories] == [
        "alice"
    ]


@pytest.mark.parametrize("late_sender", ["alice", "bob"])
def test_declined_pair_refuses_later_consent_without_lifting_cooldown(late_sender):
    """Neither participant can turn a decline into a consent after the fact."""
    declined_at = _utcnow()
    session = FakeSession(
        proposal=proposal(status="declined", declined_at=declined_at),
        people=people(),
    )

    with (
        patch("thenetwork.introductions.send_reply") as send,
        patch("thenetwork.introductions.send_proxy_introduction") as group_send,
    ):
        result = process_consent_reply(
            sender_person_id=late_sender,
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
            session_factory=factory(session),
        )

    assert result.outcome == "declined"
    assert session.proposal.status == "declined"
    assert session.proposal.declined_at == declined_at
    assert session.commits == 0
    send.assert_called_once()
    assert (
        send.call_args.kwargs["fixed_template"]
        is FixedEmailTemplate.CONSENT_ALREADY_DECLINED
    )
    group_send.assert_not_called()


def test_both_authenticated_consents_trigger_server_composed_proxy_email():
    session = FakeSession(
        proposal=proposal(person_a_consented=True, status="one_consented"),
        people=people(),
    )

    with patch("thenetwork.introductions.send_proxy_introduction") as group_send:
        result = process_consent_reply(
            sender_person_id="bob",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
            session_factory=factory(session),
        )

    assert result.outcome == "introduced"
    assert [memory.recipient_person_id for memory in result.sent_email_memories] == [
        "alice",
        "bob",
    ]
    assert len({memory.summary for memory in result.sent_email_memories}) == 1
    group_send.assert_called_once_with(
        person_a_name="Alice",
        person_a_email="alice@example.com",
        person_b_name="Bob",
        person_b_email="bob@example.com",
        reply_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        trace_id=None,
    )


def test_revoked_pair_refuses_later_consent_and_proxy_send():
    session = FakeSession(proposal=proposal(status="revoked"), people=people())

    with patch("thenetwork.introductions.send_proxy_introduction") as group_send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
            session_factory=factory(session),
        )

    assert result.outcome == "revoked"
    group_send.assert_not_called()


def test_post_introduction_revocation_is_persisted():
    session = FakeSession(
        proposal=proposal(
            person_a_consented=True,
            person_b_consented=True,
            status="introduced",
        ),
        people=people(),
    )

    with patch("thenetwork.introductions.send_proxy_introduction") as group_send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="REVOKE",
            session_factory=factory(session),
        )

    assert result.outcome == "revoked"
    assert session.proposal.status == "revoked"
    assert session.commits == 1
    group_send.assert_not_called()


def test_existing_declined_pair_cannot_be_reproposed():
    session = FakeSession(proposal=proposal(status="revoked"), people=people())

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
        )

    assert result == {"status": "suppressed", "reason": "revoked"}
    send.assert_not_called()


def test_declined_pair_is_reproposed_after_cooldown():
    old = proposal(status="declined")
    old.declined_at = _utcnow() - timedelta(days=91)
    original_token = old.reply_token
    session = FakeSession(proposal=old, people=people())

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
            decline_cooldown_days=90,
        )

    assert result == {"status": "proposed"}
    assert session.proposal.status == "proposed"
    assert session.proposal.declined_at is None
    assert session.proposal.reply_token != original_token
    assert send.call_count == 2


def test_declined_pair_stays_suppressed_during_cooldown():
    recent = proposal(status="declined")
    recent.declined_at = _utcnow() - timedelta(days=89)
    session = FakeSession(proposal=recent, people=people())

    result = propose_pair(
        sender_person_id="alice",
        other_person_id="bob",
        sender_gist="builds storage systems",
        other_gist="operates distributed databases",
        session_factory=factory(session),
        decline_cooldown_days=90,
    )

    assert result == {"status": "suppressed", "reason": "declined"}


def test_cooled_down_decline_stays_declined_when_reproposal_is_throttled():
    old = proposal(status="declined")
    old.declined_at = _utcnow() - timedelta(days=91)
    original_token = old.reply_token
    session = FakeSession(
        proposal=old,
        people=people(),
        outstanding_proposals=[proposal() for _ in range(3)],
    )

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
        )

    assert result == {
        "status": "deferred",
        "reason": "recipient_outstanding_request_cap",
        "limit": 3,
    }
    assert session.proposal.status == "declined"
    assert session.proposal.declined_at is not None
    assert session.proposal.reply_token == original_token
    send.assert_not_called()


def test_proposal_defers_when_a_recipient_has_too_many_outstanding_requests():
    session = FakeSession(
        people=people(),
        outstanding_proposals=[proposal() for _ in range(3)],
    )

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
        )

    assert result == {
        "status": "deferred",
        "reason": "recipient_outstanding_request_cap",
        "limit": 3,
    }
    send.assert_not_called()


def test_proposal_defers_when_a_recipient_reached_the_windowed_request_cap():
    session = FakeSession(
        people=people(),
        recent_proposals=[
            proposal(
                person_a_id="alice",
                person_b_id=f"near-duplicate-{number}",
                status="introduced",
            )
            for number in range(3)
        ]
        + [
            proposal(
                person_a_id="bob",
                person_b_id="someone-else",
                status="introduced",
            )
        ],
    )

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
            max_outstanding_requests_per_person=0,
            max_requests_per_person_in_window=3,
            request_window_seconds=3_600,
        )

    assert result == {
        "status": "deferred",
        "reason": "recipient_consent_request_cap",
        "limit": 3,
    }
    send.assert_not_called()


def test_fresh_counterpart_is_still_deferred_by_a_saturated_recipients_window_cap():
    """A saturated recipient's own inbound volume is bounded unconditionally: a
    fresh counterpart who has never been party to any introduction-consent row
    must not exempt the recipient's window cap, or a stream of distinct
    never-before-consented proposers could bypass it indefinitely against the
    same recipient."""
    session = FakeSession(
        people={
            "omar": Person(id="omar", name="Omar", email="omar@example.com"),
            "priya": Person(id="priya", name="Priya", email="priya@example.com"),
        },
        recent_proposals=[
            proposal(
                person_a_id="priya",
                person_b_id=f"near-duplicate-{number}",
                status="introduced",
            )
            for number in range(3)
        ],
    )

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="priya",
            other_person_id="omar",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
            max_outstanding_requests_per_person=0,
            max_requests_per_person_in_window=3,
            request_window_seconds=3_600,
        )

    assert result == {
        "status": "deferred",
        "reason": "recipient_consent_request_cap",
        "limit": 3,
    }
    send.assert_not_called()


def test_fresh_participant_is_still_deferred_by_a_saturated_counterparts_outstanding_cap():
    """Unlike the window cap above, a fresh proposer must NOT bypass a
    counterpart's saturated outstanding-request cap: those are simultaneously
    open, unresolved requests, and a stream of unrelated fresh proposers each
    getting a pass is exactly what piles up in the recipient's inbox."""
    session = FakeSession(
        people={
            "omar": Person(id="omar", name="Omar", email="omar@example.com"),
            "priya": Person(id="priya", name="Priya", email="priya@example.com"),
        },
        outstanding_proposals=[
            proposal(person_a_id="priya", person_b_id=f"near-duplicate-{number}")
            for number in range(3)
        ],
    )

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="omar",
            other_person_id="priya",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
        )

    assert result == {
        "status": "deferred",
        "reason": "recipient_outstanding_request_cap",
        "limit": 3,
    }
    send.assert_not_called()


def test_proposal_notifications_contain_only_supplied_gists_not_identity_data():
    session = FakeSession(people=people())

    with patch("thenetwork.introductions.send_reply") as send:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
        )

    assert result == {"status": "proposed"}
    assert send.call_count == 2
    contexts = [call.kwargs["fixed_context"] for call in send.call_args_list]
    assert all(isinstance(context, ConsentRequestEmailContext) for context in contexts)
    assert contexts[0].counterpart_gist == "operates distributed databases"
    assert contexts[1].counterpart_gist == "builds storage systems"
    assert all(
        context.reply_token == session.proposal.reply_token for context in contexts
    )
    assert all(
        call.kwargs["fixed_template"] is FixedEmailTemplate.CONSENT_REQUEST
        for call in send.call_args_list
    )


def test_body_token_consents_when_subject_is_from_another_thread():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_reply"):
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: An unrelated conversation",
            body="YES\n\n[INTRO:AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA]",
            session_factory=factory(session),
        )

    assert result.outcome == "one_consented"
    assert session.proposal.person_a_consented


def test_quoted_body_token_does_not_trigger_consent_handling():
    session = FakeSession(proposal=proposal(), people=people())

    result = process_consent_reply(
        sender_person_id="alice",
        sender_authenticated=True,
        subject="Re: An unrelated conversation",
        body="> [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]\n> Reply YES",
        session_factory=factory(session),
    )

    assert result == ConsentReplyResult(handled=False)
    assert not session.proposal.person_a_consented


async def test_consent_reply_is_consumed_before_model_execution():
    from thenetwork.worker.tasks import process_email

    session = FakeSession()
    session.exec = lambda _query: Result("alice")

    with (
        patch("thenetwork.worker.tasks.get_session", factory(session)),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=ConsentReplyResult(handled=True, outcome="one_consented"),
        ) as consent_handler,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            new_callable=AsyncMock,
        ) as run_agent,
    ):
        await process_email.func(
            sender_email="alice@example.com",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
        )

    consent_handler.assert_called_once()
    run_agent.assert_not_awaited()


async def test_unparseable_tokened_reply_gets_clarification_before_model():
    from thenetwork.worker.tasks import process_email

    worker_session = FakeSession()
    worker_session.exec = lambda _query: Result("alice")
    consent_session = FakeSession(proposal=proposal(), people=people())
    real_handler = partial(
        process_consent_reply,
        session_factory=factory(consent_session),
    )

    with (
        patch("thenetwork.worker.tasks.get_session", factory(worker_session)),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            side_effect=real_handler,
        ),
        patch(
            "thenetwork.worker.tasks.record_sent_email_memories",
            new_callable=AsyncMock,
        ) as record_memories,
        patch("thenetwork.introductions.send_reply") as send,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            new_callable=AsyncMock,
        ) as run_agent,
    ):
        await process_email.func(
            sender_email="alice@example.com",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="Sounds interesting.",
        )

    # The fixed clarification still goes out server-side before any model
    # runs; the sender's own prose then reaches the agent as a framed
    # remainder so the question is not simply discarded.
    send.assert_called_once()
    record_memories.assert_awaited_once()
    assert record_memories.await_args.args[0][0].recipient_person_id == "alice"
    assert (
        send.call_args.kwargs["fixed_template"]
        is FixedEmailTemplate.CONSENT_CLARIFICATION
    )
    run_agent.assert_awaited_once()
    forwarded = run_agent.await_args.kwargs["email_body"]
    assert forwarded.startswith("[System note]")
    assert "Sounds interesting." in forwarded


def test_decision_and_token_only_reply_has_no_remainder():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_reply"):
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: An unrelated conversation",
            body="YES\n\n[intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]\n",
            session_factory=factory(session),
        )

    assert result.outcome == "one_consented"
    assert result.remainder == ""


def test_substantive_yes_reply_carries_remainder_without_tokens():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_reply"):
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: An unrelated conversation",
            body=(
                "YES\n"
                "[intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]\n"
                "Also, my real interest is provenance research "
                "for museum archives.\n"
                "> quoted proposal text with a name in it"
            ),
            session_factory=factory(session),
        )

    assert result.outcome == "one_consented"
    assert result.remainder == (
        "Also, my real interest is provenance research for museum archives."
    )
    assert "[intro:" not in result.remainder


def test_clarification_reply_carries_question_as_remainder():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_reply") as send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="Before I decide, why was this introduction chosen for me?",
            session_factory=factory(session),
        )

    assert result.outcome == "clarification_sent"
    assert (
        send.call_args.kwargs["fixed_template"]
        is FixedEmailTemplate.CONSENT_CLARIFICATION
    )
    assert result.remainder == (
        "Before I decide, why was this introduction chosen for me?"
    )


@pytest.mark.parametrize(
    ("sender_person_id", "sender_authenticated"),
    [
        ("alice", False),
        (None, True),
        ("mallory", True),
    ],
)
def test_rejected_replies_carry_no_remainder(sender_person_id, sender_authenticated):
    session = FakeSession(proposal=proposal(), people=people())

    result = process_consent_reply(
        sender_person_id=sender_person_id,
        sender_authenticated=sender_authenticated,
        subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
        body="YES\nHere is some attacker-controlled text.",
        session_factory=factory(session),
    )

    assert result.outcome == "rejected"
    assert result.remainder == ""


async def test_consent_remainder_reaches_agent_after_server_handling():
    from thenetwork.worker.tasks import process_email

    worker_session = FakeSession()
    worker_session.exec = lambda _query: Result("alice")
    consent_session = FakeSession(proposal=proposal(), people=people())
    real_handler = partial(
        process_consent_reply,
        session_factory=factory(consent_session),
    )

    with (
        patch("thenetwork.worker.tasks.get_session", factory(worker_session)),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            side_effect=real_handler,
        ),
        patch(
            "thenetwork.worker.tasks.record_sent_email_memories",
            new_callable=AsyncMock,
        ),
        patch("thenetwork.introductions.send_reply") as send,
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            new_callable=AsyncMock,
        ) as run_agent,
    ):
        await process_email.func(
            sender_email="alice@example.com",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body=(
                "YES\n\n"
                "Also, my real interest is provenance research "
                "for museum archives."
            ),
        )

    # The consent transition and fixed acknowledgment stay server-side.
    assert consent_session.proposal.person_a_consented
    send.assert_called_once()
    assert (
        send.call_args.kwargs["fixed_template"]
        is FixedEmailTemplate.CONSENT_ACKNOWLEDGMENT
    )
    # Only the framed leftover text reaches the agent, never the token.
    run_agent.assert_awaited_once()
    forwarded = run_agent.await_args.kwargs["email_body"]
    assert forwarded.startswith("[System note]")
    assert "outcome: one_consented" in forwarded
    assert "provenance research" in forwarded
    assert "[intro:" not in forwarded
    assert "YES" not in forwarded
