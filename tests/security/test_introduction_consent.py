from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from unittest.mock import AsyncMock, patch

import pytest

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.introductions import (
    CONSENT_ACKNOWLEDGMENT_REPLY,
    CONSENT_DECLINED_REPLY,
    CONSENT_CLARIFICATION_REPLY,
    ConsentReplyResult,
    _reply_action,
    process_consent_reply,
    propose_pair,
    _utcnow,
)


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
        rendered = str(query)
        if "created_at >=" in rendered:
            return Result(None, self.recent_proposals)
        if "status IN" in rendered:
            return Result(None, self.outstanding_proposals)
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

    with patch("thenetwork.introductions.send_group_introduction") as group_send:
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
        patch("thenetwork.introductions.send_group_introduction") as group_send,
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
    assert send.call_args.kwargs["body_text"] == CONSENT_ACKNOWLEDGMENT_REPLY


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
    assert send.call_args.kwargs["body_text"] == CONSENT_CLARIFICATION_REPLY


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
    assert send.call_args.kwargs["body_text"] == CONSENT_DECLINED_REPLY


def test_both_authenticated_consents_trigger_server_composed_group_email():
    session = FakeSession(
        proposal=proposal(person_a_consented=True, status="one_consented"),
        people=people(),
    )

    with patch("thenetwork.introductions.send_group_introduction") as group_send:
        result = process_consent_reply(
            sender_person_id="bob",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
            session_factory=factory(session),
        )

    assert result.outcome == "introduced"
    group_send.assert_called_once_with(
        person_a_name="Alice",
        person_a_email="alice@example.com",
        person_b_name="Bob",
        person_b_email="bob@example.com",
        trace_id=None,
    )


def test_revoked_pair_refuses_later_consent_and_group_send():
    session = FakeSession(proposal=proposal(status="revoked"), people=people())

    with patch("thenetwork.introductions.send_group_introduction") as group_send:
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

    with patch("thenetwork.introductions.send_group_introduction") as group_send:
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
        sender_person_id="alice", other_person_id="bob",
        sender_gist="builds storage systems", other_gist="operates distributed databases",
        session_factory=factory(session), decline_cooldown_days=90,
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
    bodies = [call.kwargs["body_text"] for call in send.call_args_list]
    assert "Bob" not in bodies[0]
    assert "bob@example.com" not in bodies[0]
    assert "Alice" not in bodies[1]
    assert "alice@example.com" not in bodies[1]
    token = f"[intro:{session.proposal.reply_token}]"
    assert all(token in body for body in bodies)


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

    with patch("thenetwork.worker.tasks.get_session", factory(session)), \
         patch("thenetwork.worker.tasks.check_rate_limit", return_value=True), \
         patch("thenetwork.worker.tasks.scan_content", return_value=(True, None)), \
         patch("thenetwork.worker.tasks.verify_admin_request", return_value=None), \
         patch(
             "thenetwork.worker.tasks.process_consent_reply",
             return_value=ConsentReplyResult(handled=True, outcome="one_consented"),
         ) as consent_handler, \
         patch(
             "thenetwork.worker.tasks.run_agent_for_email",
             new_callable=AsyncMock,
         ) as run_agent:
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

    run_agent.assert_not_awaited()
    send.assert_called_once()
    assert send.call_args.kwargs["body_text"] == CONSENT_CLARIFICATION_REPLY
