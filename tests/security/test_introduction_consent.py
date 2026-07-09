from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.introductions import process_consent_reply, propose_pair
from thenetwork.introductions import ConsentReplyResult


class Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, proposal=None, people=None):
        self.proposal = proposal
        self.people = people or {}
        self.added = []
        self.commits = 0

    def exec(self, _query):
        return Result(self.proposal)

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


def test_one_sided_consent_never_sends_revealing_email():
    session = FakeSession(proposal=proposal(), people=people())

    with patch("thenetwork.introductions.send_group_introduction") as group_send:
        result = process_consent_reply(
            sender_person_id="alice",
            sender_authenticated=True,
            subject="Re: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]",
            body="YES",
            session_factory=factory(session),
        )

    assert result.outcome == "one_consented"
    assert session.proposal.person_a_consented
    assert not session.proposal.person_b_consented
    group_send.assert_not_called()


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
