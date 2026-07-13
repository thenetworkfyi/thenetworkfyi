from contextlib import contextmanager
from unittest.mock import patch

import pytest

from thenetwork.db.models import PendingIntroCandidate, Person
from thenetwork.introductions import (
    DIGEST_ALREADY_RESOLVED_REPLY,
    DIGEST_CLARIFICATION_REPLY,
    DIGEST_NONE_SELECTED_REPLY,
    DigestReplyResult,
    _digest_selection,
    flush_pending_digests,
    process_digest_reply,
    propose_pair,
    queue_intro_candidate,
)


class Result:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None


class FakeSession:
    """Minimal fake for digest-queue queries: each call site issues exactly
    one query shape, so no SQL sniffing is needed (unlike the consent-side
    FakeSession, which juggles several IntroductionConsent query shapes)."""

    def __init__(self, rows=None, people=None):
        self.rows = rows or []
        self.people = people or {}
        self.added = []
        self.commits = 0

    def exec(self, _query):
        return Result(self.rows)

    def get(self, _model, person_id):
        return self.people.get(person_id)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1


def factory(session):
    @contextmanager
    def open_session():
        yield session

    return open_session


def person(person_id, name):
    return Person(id=person_id, name=name, email=f"{person_id}@example.com")


def candidate_row(**overrides):
    values = {
        "recipient_person_id": "bob",
        "candidate_person_id": "alice",
        "recipient_gist": "operates distributed databases",
        "candidate_gist": "builds storage systems",
        "status": "digested",
        "digest_token": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "label": "A",
    }
    values.update(overrides)
    return PendingIntroCandidate(**values)


# --- propose_pair(queue_on_cap=...) -----------------------------------------


def test_propose_pair_queues_the_capped_side_when_outstanding_cap_reached():
    from thenetwork.db.models import IntroductionConsent

    outstanding = [
        IntroductionConsent(person_a_id="bob", person_b_id=f"other-{n}")
        for n in range(3)
    ]

    class ConsentFakeSession:
        def __init__(self):
            self.commits = 0

        def exec(self, query):
            rendered = str(query.compile(compile_kwargs={"literal_binds": True}))
            if "status IN" in rendered and "'bob'" in rendered:
                return Result(outstanding)
            return Result([])

        def get(self, _model, person_id):
            return {
                "alice": person("alice", "Alice"),
                "bob": person("bob", "Bob"),
            }.get(person_id)

        def add(self, _value):
            pass

        def commit(self):
            self.commits += 1

    session = ConsentFakeSession()
    session_factory = factory(session)

    with (
        patch("thenetwork.introductions.send_reply") as send,
        patch("thenetwork.introductions.queue_intro_candidate") as queue_mock,
    ):
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=session_factory,
            queue_on_cap=True,
        )

    assert result == {
        "status": "deferred",
        "reason": "recipient_outstanding_request_cap",
        "limit": 3,
    }
    send.assert_not_called()
    queue_mock.assert_called_once_with(
        recipient_person_id="bob",
        candidate_person_id="alice",
        recipient_gist="operates distributed databases",
        candidate_gist="builds storage systems",
        session_factory=session_factory,
    )


def test_propose_pair_does_not_queue_by_default():
    from thenetwork.db.models import IntroductionConsent

    outstanding = [
        IntroductionConsent(person_a_id="bob", person_b_id=f"other-{n}")
        for n in range(3)
    ]

    class ConsentFakeSession:
        def exec(self, query):
            rendered = str(query.compile(compile_kwargs={"literal_binds": True}))
            if "status IN" in rendered and "'bob'" in rendered:
                return Result(outstanding)
            return Result([])

        def get(self, _model, person_id):
            return {
                "alice": person("alice", "Alice"),
                "bob": person("bob", "Bob"),
            }.get(person_id)

        def add(self, _value):
            pass

        def commit(self):
            pass

    session = ConsentFakeSession()

    with patch("thenetwork.introductions.queue_intro_candidate") as queue_mock:
        result = propose_pair(
            sender_person_id="alice",
            other_person_id="bob",
            sender_gist="builds storage systems",
            other_gist="operates distributed databases",
            session_factory=factory(session),
        )

    assert result["status"] == "deferred"
    queue_mock.assert_not_called()


# --- flush_pending_digests ---------------------------------------------------


def test_flush_batches_queued_candidates_into_one_digest_capped_and_labeled():
    rows = [
        PendingIntroCandidate(
            recipient_person_id="bob",
            candidate_person_id=f"cand-{n}",
            recipient_gist="bob's own gist",
            candidate_gist=f"candidate {n} gist",
            status="queued",
        )
        for n in range(4)
    ] + [
        PendingIntroCandidate(
            recipient_person_id="alice",
            candidate_person_id="cand-x",
            recipient_gist="alice's own gist",
            candidate_gist="candidate x gist",
            status="queued",
        )
    ]
    session = FakeSession(
        rows=rows,
        people={"bob": person("bob", "Bob"), "alice": person("alice", "Alice")},
    )

    with patch("thenetwork.introductions.send_reply") as send:
        result = flush_pending_digests(session_factory=factory(session))

    assert result == {"digests_sent": 2}
    assert send.call_count == 2

    bob_call = next(
        c for c in send.call_args_list if c.kwargs["to_address"] == "bob@example.com"
    )
    body = bob_call.kwargs["body_text"]
    assert "A. candidate 0 gist" in body
    assert "B. candidate 1 gist" in body
    assert "C. candidate 2 gist" in body
    # Default introduction_digest_size=3 caps the batch below the hard max of 4.
    assert "candidate 3 gist" not in body
    assert "Bob" not in body
    assert "bob@example.com" not in body

    digested = [
        r for r in rows if r.recipient_person_id == "bob" and r.status == "digested"
    ]
    assert len(digested) == 3
    assert {r.label for r in digested} == {"A", "B", "C"}
    still_queued = [
        r for r in rows if r.recipient_person_id == "bob" and r.status == "queued"
    ]
    assert len(still_queued) == 1


def test_flush_does_nothing_when_no_candidates_queued():
    session = FakeSession(rows=[], people={})

    with patch("thenetwork.introductions.send_reply") as send:
        result = flush_pending_digests(session_factory=factory(session))

    assert result == {"digests_sent": 0}
    send.assert_not_called()


# --- queue_intro_candidate ----------------------------------------------------


def test_queue_intro_candidate_rejects_self_and_missing_ids():
    session = FakeSession()
    assert queue_intro_candidate(
        recipient_person_id="alice",
        candidate_person_id="alice",
        recipient_gist="g",
        candidate_gist="g",
        session_factory=factory(session),
    ) == {"status": "error", "reason": "self_introduction"}


def test_queue_intro_candidate_dedups_existing_pair():
    existing = PendingIntroCandidate(
        recipient_person_id="bob",
        candidate_person_id="alice",
        recipient_gist="g",
        candidate_gist="g",
        status="queued",
    )

    class DedupSession(FakeSession):
        def exec(self, query):
            rendered = str(query.compile(compile_kwargs={"literal_binds": True}))
            if "introduction_consents" in rendered:
                return Result([])
            return Result([existing])

    session = DedupSession(rows=[existing])
    result = queue_intro_candidate(
        recipient_person_id="bob",
        candidate_person_id="alice",
        recipient_gist="g",
        candidate_gist="g",
        session_factory=factory(session),
    )
    assert result == {"status": "already_queued", "candidate_status": "queued"}


# --- process_digest_reply -----------------------------------------------------


def test_process_digest_reply_no_token_is_not_handled():
    result = process_digest_reply(
        sender_person_id="bob",
        sender_authenticated=True,
        subject="Re: something else",
        body="A",
        session_factory=factory(FakeSession()),
    )
    assert result == DigestReplyResult(handled=False)


def test_process_digest_reply_rejects_unauthenticated_sender():
    rows = [candidate_row()]
    session = FakeSession(rows=rows, people={"bob": person("bob", "Bob")})

    result = process_digest_reply(
        sender_person_id="bob",
        sender_authenticated=False,
        subject="Re: Possible introductions [digest:dddddddd-dddd-dddd-dddd-dddddddddddd]",
        body="A",
        session_factory=factory(session),
    )
    assert result.handled
    assert result.outcome == "rejected"


def test_process_digest_reply_rejects_non_owner():
    rows = [candidate_row(recipient_person_id="bob")]
    session = FakeSession(rows=rows, people={"mallory": person("mallory", "Mallory")})

    result = process_digest_reply(
        sender_person_id="mallory",
        sender_authenticated=True,
        subject="Re: [digest:dddddddd-dddd-dddd-dddd-dddddddddddd]",
        body="A",
        session_factory=factory(session),
    )
    assert result.handled
    assert result.outcome == "rejected"


def test_process_digest_reply_already_resolved_when_no_digested_rows_remain():
    rows = [candidate_row(status="selected")]
    session = FakeSession(rows=rows, people={"bob": person("bob", "Bob")})

    with patch("thenetwork.introductions.send_reply") as send:
        result = process_digest_reply(
            sender_person_id="bob",
            sender_authenticated=True,
            subject="Re: [digest:dddddddd-dddd-dddd-dddd-dddddddddddd]",
            body="A",
            session_factory=factory(session),
        )

    assert result.outcome == "already_resolved"
    assert send.call_args.kwargs["body_text"] == DIGEST_ALREADY_RESOLVED_REPLY


def test_process_digest_reply_clarification_on_unparseable_body():
    rows = [candidate_row()]
    session = FakeSession(rows=rows, people={"bob": person("bob", "Bob")})

    with patch("thenetwork.introductions.send_reply") as send:
        result = process_digest_reply(
            sender_person_id="bob",
            sender_authenticated=True,
            subject="Re: [digest:dddddddd-dddd-dddd-dddd-dddddddddddd]",
            body="I'm not sure yet",
            session_factory=factory(session),
        )

    assert result.outcome == "clarification_sent"
    assert send.call_args.kwargs["body_text"] == DIGEST_CLARIFICATION_REPLY


def test_process_digest_reply_none_selected_marks_all_rows_not_selected():
    rows = [
        candidate_row(label="A", candidate_person_id="alice"),
        candidate_row(label="B", candidate_person_id="carol"),
    ]
    session = FakeSession(rows=rows, people={"bob": person("bob", "Bob")})

    with (
        patch("thenetwork.introductions.send_reply") as send,
        patch("thenetwork.introductions.propose_pair") as propose_mock,
    ):
        result = process_digest_reply(
            sender_person_id="bob",
            sender_authenticated=True,
            subject="Re: [digest:dddddddd-dddd-dddd-dddd-dddddddddddd]",
            body="NONE",
            session_factory=factory(session),
        )

    assert result.outcome == "none_selected"
    assert send.call_args.kwargs["body_text"] == DIGEST_NONE_SELECTED_REPLY
    assert all(row.status == "not_selected" for row in rows)
    propose_mock.assert_not_called()


def test_process_digest_reply_sends_intro_requests_only_for_selected_letters():
    rows = [
        candidate_row(
            label="A", candidate_person_id="alice", candidate_gist="alice gist"
        ),
        candidate_row(
            label="B", candidate_person_id="carol", candidate_gist="carol gist"
        ),
        candidate_row(
            label="C", candidate_person_id="dave", candidate_gist="dave gist"
        ),
    ]
    session = FakeSession(rows=rows, people={"bob": person("bob", "Bob")})

    with (
        patch("thenetwork.introductions.send_reply"),
        patch(
            "thenetwork.introductions.propose_pair", return_value={"status": "proposed"}
        ) as propose_mock,
    ):
        result = process_digest_reply(
            sender_person_id="bob",
            sender_authenticated=True,
            subject="Re: [digest:dddddddd-dddd-dddd-dddd-dddddddddddd]",
            body="A, C",
            session_factory=factory(session),
        )

    assert result.outcome == "selected"
    assert propose_mock.call_count == 2
    called_others = {c.kwargs["other_person_id"] for c in propose_mock.call_args_list}
    assert called_others == {"alice", "dave"}
    statuses = {row.label: row.status for row in rows}
    assert statuses == {"A": "selected", "B": "not_selected", "C": "selected"}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("A", {"A"}),
        ("A, C", {"A", "C"}),
        ("a and b", {"A", "B"}),
        ("none", set()),
        ("NONE.", set()),
        ("maybe later", None),
    ],
)
def test_digest_selection_parsing(body, expected):
    assert _digest_selection(body) == expected
