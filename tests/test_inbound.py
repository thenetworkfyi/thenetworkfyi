"""Regression guard: inbound IMAP polling never archives or deletes mail.

poll_unseen() and mark_messages_seen() are the only two places that talk to
the INBOX mailbox. This suite mocks imap-tools' MailBox and asserts both
functions only ever fetch/flag messages - never move, delete, expunge, or
copy them out of INBOX - so a future change can't silently start archiving
or deleting inbound mail.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from imap_tools import MailMessageFlags

from thenetwork.email import inbound
from thenetwork.settings import Settings


def _settings(
    *,
    require_sender_auth: bool = False,
    trusted_authserv_id: str = "",
) -> Settings:
    return Settings(
        imap_account="agent@example.com",
        imap_password="secret",
        imap_host="imap.example.com",
        imap_port=993,
        require_sender_auth=require_sender_auth,
        trusted_authserv_id=trusted_authserv_id,
    )


def _fake_message(
    uid: str = "1",
    from_: str = "alice@example.com",
    subject: str = "hello",
    body_text: str = "hello there",
    headers: dict | None = None,
    from_display_name: str = "",
):
    msg = MagicMock()
    msg.uid = uid
    msg.from_ = from_
    msg.subject = subject
    msg.headers = headers or {}
    msg.text = body_text
    msg.html = ""
    # Mirrors imap_tools.utils.parse_email_addresses(): a bare address with no
    # display name yields EmailAddress(name='', email=...) - empty string, not
    # None.
    msg.from_values = SimpleNamespace(name=from_display_name, email=from_)
    return msg


class _FakeMailBox:
    """Stands in for imap_tools.MailBox.

    Only exposes the calls poll_unseen()/mark_messages_seen() are allowed to
    make (fetch, flag) plus the mutating ones they must never make (move,
    delete, expunge, copy), all as spies so tests can assert on them.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.fetch = MagicMock(return_value=[])
        self.flag = MagicMock()
        self.move = MagicMock()
        self.delete = MagicMock()
        self.expunge = MagicMock()
        self.copy = MagicMock()

    def login(self, user: str, password: str) -> "_FakeMailBox":
        return self

    def __enter__(self) -> "_FakeMailBox":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


@pytest.fixture
def fake_mailbox(monkeypatch: pytest.MonkeyPatch) -> _FakeMailBox:
    box = _FakeMailBox("imap.example.com", 993)
    monkeypatch.setattr(inbound, "MailBox", lambda host, port: box)
    monkeypatch.setattr(inbound, "get_settings", _settings)
    return box


def _assert_never_mutates_inbox(box: _FakeMailBox) -> None:
    box.move.assert_not_called()
    box.delete.assert_not_called()
    box.expunge.assert_not_called()
    box.copy.assert_not_called()


def test_poll_unseen_fetches_with_mark_seen_false(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [_fake_message()]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    fake_mailbox.fetch.assert_called_once()
    _, kwargs = fake_mailbox.fetch.call_args
    assert kwargs["mark_seen"] is False


def test_poll_unseen_caps_subject(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(subject="s" * (inbound.MAX_SUBJECT_CHARS + 10))
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].subject == "s" * inbound.MAX_SUBJECT_CHARS


def test_poll_unseen_captures_message_id(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(headers={"message-id": ["<abc123@example.com>"]})
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].message_id == "<abc123@example.com>"


def test_poll_unseen_strips_message_id(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(headers={"message-id": ["  <abc123@example.com>  "]})
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].message_id == "<abc123@example.com>"


def test_poll_unseen_captures_references(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={"references": ["<root@example.com> <parent@example.com>"]}
        )
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].message_references == "<root@example.com> <parent@example.com>"


def test_poll_unseen_rejects_unsafe_threading_headers(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={
                "message-id": ["<abc 123@example.com>"],
                "references": ["<root@example.com>\r\n <parent@example.com>"],
            }
        )
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].message_id is None
    assert messages[0].message_references is None


def test_poll_unseen_captures_message_date(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(headers={"date": ["Sat, 04 Jul 2026 12:00:00 -0700"]})
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].message_date == "Sat, 04 Jul 2026 12:00:00 -0700"


def test_poll_unseen_captures_sender_display_name(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(from_="first.last@example.com", from_display_name="First Last")
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].sender_display_name == "First Last"


def test_poll_unseen_treats_missing_display_name_as_none(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(from_="alice@example.com", from_display_name="")
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].sender_display_name is None


def test_poll_unseen_strips_and_caps_display_name(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(from_display_name="  " + "n" * (inbound.MAX_SENDER_NAME_CHARS + 10) + "  ")
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].sender_display_name == "n" * inbound.MAX_SENDER_NAME_CHARS


def test_poll_unseen_mints_opaque_trace_ids(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(uid="1"),
        _fake_message(uid="2", from_="bob@example.com"),
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 2
    assert messages[0].trace_id != messages[1].trace_id
    for message in messages:
        parsed = UUID(message.trace_id, version=4)
        assert str(parsed) == message.trace_id


def test_poll_unseen_accepts_purelymail_auth_pass(
    monkeypatch: pytest.MonkeyPatch,
    fake_mailbox: _FakeMailBox,
):
    monkeypatch.setattr(
        inbound,
        "get_settings",
        lambda: _settings(
            require_sender_auth=True, trusted_authserv_id="purelymail.com"
        ),
    )
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={"authentication-results": ["purelymail.com; auth=pass"]}
        )
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].sender_authenticated is True


@pytest.mark.parametrize(
    "header",
    [
        "mx.example.com; dkim=pass header.d=example.com",
        "mx.example.com; spf=pass smtp.mailfrom=example.com",
        "mx.example.com; auth=pass",
    ],
)
def test_is_sender_authenticated_accepts_passing_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    header: str,
):
    monkeypatch.setattr(
        inbound, "get_settings", lambda: _settings(require_sender_auth=True)
    )
    msg = SimpleNamespace(headers={"authentication-results": [header]})

    assert inbound._is_sender_authenticated(msg) is True


def test_is_sender_authenticated_uses_nearest_authentication_results(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        inbound, "get_settings", lambda: _settings(require_sender_auth=True)
    )
    msg = SimpleNamespace(
        headers={
            "authentication-results": [
                "mx.example.com; auth=fail",
                "mx.example.com; auth=pass",
            ]
        }
    )

    assert inbound._is_sender_authenticated(msg) is False


def test_poll_unseen_does_not_reject_near_empty_body(fake_mailbox: _FakeMailBox):
    """Near-empty bodies must reach process_email, not be dropped at intake -

    worker/tasks.py handles them (rate limit + first-contact welcome reply),
    and a legitimate short first email (e.g. just "Hi") must not be silently
    discarded with no reply at all.
    """
    fake_mailbox.fetch.return_value = [_fake_message(body_text=" \n")]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].rejection_reason is None


def test_poll_unseen_marks_oversized_body_as_rejected(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(body_text="a" * (inbound.MAX_RAW_BODY_CHARS + 1))
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].body == ""
    assert messages[0].rejection_reason == inbound.REJECT_BODY_OVERSIZE
    assert messages[0].body_chars > inbound.MAX_RAW_BODY_CHARS


def test_poll_unseen_never_mutates_the_mailbox(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [_fake_message()]

    inbound.poll_unseen()

    _assert_never_mutates_inbox(fake_mailbox)
    # poll_unseen() must not flag messages either - that is mark_messages_seen()'s job,
    # called only after successful enqueue.
    fake_mailbox.flag.assert_not_called()


def test_mark_messages_seen_only_sets_seen_flag(fake_mailbox: _FakeMailBox):
    inbound.mark_messages_seen(["1", "2"])

    fake_mailbox.flag.assert_called_once()
    args, _ = fake_mailbox.flag.call_args
    uids, flags, value = args[0], args[1], args[2]
    assert list(uids) == ["1", "2"]
    assert list(flags) == [MailMessageFlags.SEEN]
    assert value is True


def test_mark_messages_seen_never_mutates_the_mailbox(fake_mailbox: _FakeMailBox):
    inbound.mark_messages_seen(["1", "2"])

    _assert_never_mutates_inbox(fake_mailbox)


def test_mark_messages_seen_with_no_uids_does_not_touch_the_mailbox(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(inbound, "get_settings", _settings)
    mailbox_ctor = MagicMock()
    monkeypatch.setattr(inbound, "MailBox", mailbox_ctor)

    inbound.mark_messages_seen([])

    mailbox_ctor.assert_not_called()
