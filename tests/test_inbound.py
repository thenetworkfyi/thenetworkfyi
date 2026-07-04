"""Regression guard: inbound IMAP polling never archives or deletes mail.

poll_unseen() and mark_messages_seen() are the only two places that talk to
the INBOX mailbox. This suite mocks imap-tools' MailBox and asserts both
functions only ever fetch/flag messages - never move, delete, expunge, or
copy them out of INBOX - so a future change can't silently start archiving
or deleting inbound mail.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from imap_tools import MailMessageFlags

from thenetwork.email import inbound
from thenetwork.settings import Settings


def _settings() -> Settings:
    return Settings(
        imap_account="agent@example.com",
        imap_password="secret",
        imap_host="imap.example.com",
        imap_port=993,
        require_sender_auth=False,
    )


def _fake_message(
    uid: str = "1",
    from_: str = "alice@example.com",
    subject: str = "hello",
    body_text: str = "hello there",
    headers: dict | None = None,
):
    msg = MagicMock()
    msg.uid = uid
    msg.from_ = from_
    msg.subject = subject
    msg.headers = headers or {}
    msg.text = body_text
    msg.html = ""
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


def test_poll_unseen_marks_empty_body_as_rejected(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [_fake_message(body_text=" \n")]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].rejection_reason == inbound.REJECT_BODY_EMPTY


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
