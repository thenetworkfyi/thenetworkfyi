"""Regression guard: inbound IMAP polling never archives or deletes mail.

poll_unseen() and mark_messages_seen() are the only two places that talk to
the INBOX mailbox. This suite mocks imap-tools' MailBox and asserts both
functions only ever fetch/flag messages - never move, delete, expunge, or
copy them out of INBOX - so a future change can't silently start archiving
or deleting inbound mail.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from imap_tools import MailMessageFlags

from thenetwork.audit import LOGGER_NAME
from thenetwork.email import inbound
from thenetwork.settings import Settings


def _settings(
    *,
    require_sender_auth: bool = False,
) -> Settings:
    return Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
        imap_account="agent@example.com",
        imap_password="secret",
        imap_host="imap.example.com",
        imap_port=993,
        require_sender_auth=require_sender_auth,
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
        self.login = MagicMock(return_value=self)

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


def _audit_events(caplog) -> list[dict]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


def test_poll_unseen_fetches_with_mark_seen_false(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [_fake_message()]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    fake_mailbox.fetch.assert_called_once()
    _, kwargs = fake_mailbox.fetch.call_args
    assert kwargs["mark_seen"] is False


def test_poll_unseen_uses_separate_relay_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = _settings()
    settings.relay_imap_account = "relay@relay.example.com"
    settings.relay_imap_password = "relay-secret"
    box = _FakeMailBox(settings.imap_host, settings.imap_port)
    monkeypatch.setattr(inbound, "MailBox", lambda host, port: box)
    monkeypatch.setattr(inbound, "get_settings", lambda: settings)

    inbound.poll_unseen(mailbox="relay")

    box.login.assert_called_once_with(
        settings.relay_imap_account, settings.relay_imap_password
    )


def test_relay_mailbox_configuration_requires_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = _settings()
    settings.relay_imap_account = "relay@relay.example.com"
    monkeypatch.setattr(inbound, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="must both be configured"):
        inbound.relay_mailbox_configured()


def test_poll_unseen_caps_subject(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(subject="s" * (inbound.MAX_SUBJECT_CHARS + 10))
    ]

    messages = inbound.poll_unseen()

    assert len(messages) == 1
    assert messages[0].subject == "s" * inbound.MAX_SUBJECT_CHARS


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(
            {"to": ["person@example.com"], "cc": ["AGENT@EXAMPLE.COM"]},
            True,
            id="service_only_in_cc",
        ),
        pytest.param(
            {"to": ["The Network <agent@example.com>"], "cc": ["agent@example.com"]},
            False,
            id="service_also_in_to",
        ),
        pytest.param(
            {"to": ["person@example.com"], "cc": ["other@example.com"]},
            False,
            id="service_absent_from_both",
        ),
        pytest.param(
            {
                "to": ["person@example.com, The Network <agent@example.com>"],
                "cc": ["agent@example.com"],
            },
            False,
            id="service_among_multiple_to_recipients",
        ),
    ],
)
def test_poll_unseen_classifies_cc_only_service_recipient(
    fake_mailbox: _FakeMailBox, headers: dict[str, list[str]], expected: bool
):
    fake_mailbox.fetch.return_value = [_fake_message(headers=headers)]

    [message] = inbound.poll_unseen()

    assert message.cc_only_service_recipient is expected


@pytest.mark.parametrize(
    "service_address",
    ["agent@example.com", "outbound@example.com", "relay@relay.example.com"],
)
def test_poll_unseen_derives_all_service_addresses_from_settings(
    fake_mailbox: _FakeMailBox, service_address: str
):
    settings = _settings()
    settings.email_from = "outbound@example.com"
    settings.relay_imap_account = "relay@relay.example.com"
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={
                "to": ["person@example.com"],
                "cc": [f"The Network <{service_address}>"],
            }
        )
    ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(inbound, "get_settings", lambda: settings)
        [message] = inbound.poll_unseen()

    assert message.cc_only_service_recipient is True


def test_poll_unseen_captures_dovecot_catchall_recipient(
    fake_mailbox: _FakeMailBox,
):
    token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    proxy = f"hidden-{token}@relay.example.com"
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={
                "to": ["public@example.com, " + proxy],
                "x-original-to": [proxy],
            }
        )
    ]
    settings = _settings()
    settings.relay_domain = "relay.example.com"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(inbound, "get_settings", lambda: settings)
        messages = inbound.poll_unseen()

    assert messages[0].recipient_address == proxy


def test_poll_unseen_preserves_raw_mime_for_relay_candidate(
    fake_mailbox: _FakeMailBox,
):
    token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    proxy = f"hidden-{token}@relay.example.com"
    message = _fake_message(headers={"x-original-to": [proxy]})
    message.raw_message_bytes = b"original multipart bytes"
    fake_mailbox.fetch.return_value = [message]
    settings = _settings()
    settings.relay_domain = "relay.example.com"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(inbound, "get_settings", lambda: settings)
        messages = inbound.poll_unseen()

    assert messages[0].raw_message == b"original multipart bytes"


def test_poll_unseen_prefers_hidden_alias_over_same_domain_catchall(
    fake_mailbox: _FakeMailBox,
):
    token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    proxy = f"hidden-{token}@relay.example.com"
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={
                "delivered-to": ["catchall@relay.example.com"],
                "envelope-to": ["mailbox@relay.example.com"],
                "to": [f"The Network <{proxy}>"],
            }
        )
    ]
    settings = _settings()
    settings.relay_domain = "relay.example.com"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(inbound, "get_settings", lambda: settings)
        messages = inbound.poll_unseen()

    assert messages[0].recipient_address == proxy


@pytest.mark.parametrize(
    "proxy",
    [
        "hidden-not-a-token@relay.example.com",
        "hidden-AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA@relay.example.com",
    ],
)
def test_poll_unseen_preserves_invalid_hidden_candidate_for_fail_closed_worker(
    fake_mailbox: _FakeMailBox,
    proxy: str,
):
    fake_mailbox.fetch.return_value = [
        _fake_message(
            headers={
                "delivered-to": ["catchall@relay.example.com"],
                "to": [proxy],
            }
        )
    ]
    settings = _settings()
    settings.relay_domain = "relay.example.com"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(inbound, "get_settings", lambda: settings)
        messages = inbound.poll_unseen()

    assert messages[0].recipient_address == proxy


def test_poll_unseen_drops_oversized_recipient(fake_mailbox: _FakeMailBox):
    fake_mailbox.fetch.return_value = [
        _fake_message(headers={"to": ["a" * 321 + "@example.com"]})
    ]

    messages = inbound.poll_unseen()

    assert messages[0].recipient_address is None


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
        _fake_message(
            from_display_name="  " + "n" * (inbound.MAX_SENDER_NAME_CHARS + 10) + "  "
        )
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
        lambda: _settings(require_sender_auth=True),
    )
    fake_mailbox.fetch.return_value = [
        _fake_message(headers={"authentication-results": ["purelymail.com; auth=pass"]})
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


def test_is_sender_authenticated_warns_once_for_unrecognized_auth_header(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
):
    inbound._WARNED_UNRECOGNIZED_AUTH_RESULTS.clear()
    monkeypatch.setattr(
        inbound, "get_settings", lambda: _settings(require_sender_auth=True)
    )
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    msg = SimpleNamespace(
        headers={
            "authentication-results": [
                "mx.example.com; arc=pass x-provider=pass user=alice@example.com"
            ]
        }
    )

    assert inbound._is_sender_authenticated(msg) is False
    assert inbound._is_sender_authenticated(msg) is False

    events = _audit_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "email.auth_header_unrecognized"
    assert events[0]["authserv_id"] == "mx.example.com"
    assert events[0]["auth_result_mechanisms"] == [
        "arc",
        "user",
        "x-provider",
    ]
    assert caplog.records[0].levelname == "WARNING"
    assert "alice@example.com" not in caplog.records[0].message
    assert "arc=pass" not in caplog.records[0].message


@pytest.mark.parametrize(
    "header",
    [
        "mx.example.com; dkim=fail header.d=example.com",
        "mx.example.com; spf=none smtp.mailfrom=example.com",
        "mx.example.com; auth=fail",
    ],
)
def test_is_sender_authenticated_does_not_warn_for_known_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    header: str,
):
    inbound._WARNED_UNRECOGNIZED_AUTH_RESULTS.clear()
    monkeypatch.setattr(
        inbound, "get_settings", lambda: _settings(require_sender_auth=True)
    )
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    msg = SimpleNamespace(headers={"authentication-results": [header]})

    assert inbound._is_sender_authenticated(msg) is False

    assert _audit_events(caplog) == []


def test_poll_unseen_does_not_reject_near_empty_body(fake_mailbox: _FakeMailBox):
    """Near-empty bodies must reach process_email, not be dropped at intake -

    the worker applies the ordinary safety gates and lets the agent decide
    whether a short first email needs the fixed welcome or a substantive reply.
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
