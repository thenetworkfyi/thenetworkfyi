"""Tests for inbound email body extraction (imap-tools + BeautifulSoup)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from thenetwork.email import inbound
from thenetwork.email.inbound import MAX_BODY_CHARS, _html_to_text
from thenetwork.settings import Settings


def _settings() -> Settings:
    return Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
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
    text: str = "",
    html: str = "",
):
    msg = MagicMock()
    msg.uid = uid
    msg.from_ = from_
    msg.subject = subject
    msg.headers = {}
    msg.text = text
    msg.html = html
    return msg


class _FakeMailBox:
    """Minimal stand-in for imap_tools.MailBox, enough to drive poll_unseen()."""

    def __init__(self, host: str, port: int) -> None:
        self.fetch = MagicMock(return_value=[])

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


def _poll_body(fake_mailbox: _FakeMailBox, **message_kwargs) -> str:
    fake_mailbox.fetch.return_value = [_fake_message(**message_kwargs)]
    messages = inbound.poll_unseen()
    assert len(messages) == 1
    return messages[0].body


def test_plain_text_is_preferred_over_html(fake_mailbox: _FakeMailBox):
    body = _poll_body(fake_mailbox, text="plain body", html="<p>HTML body</p>")

    assert body == "plain body"


def test_html_only_body_is_reduced_to_visible_text(fake_mailbox: _FakeMailBox):
    body = _poll_body(
        fake_mailbox,
        text="",
        html=(
            "<html><head><title>hidden</title><style>.x { color: red }</style></head>"
            "<body><p>Hello <b>there</b></p><script>steal()</script></body></html>"
        ),
    )

    assert body == "Hello there"


def test_body_is_bounded_to_max_body_chars(fake_mailbox: _FakeMailBox):
    body = _poll_body(fake_mailbox, text="a" * (MAX_BODY_CHARS + 100))

    assert body == "a" * MAX_BODY_CHARS


def test_html_to_text_strips_head_script_style_template_title():
    html = (
        "<html><head><title>hidden title</title></head>"
        "<body>"
        "<template>hidden template</template>"
        "<style>.x { color: red }</style>"
        "<script>steal()</script>"
        "<p>Hello <b>there</b></p>"
        "</body></html>"
    )

    assert _html_to_text(html) == "Hello there"


def test_html_to_text_normalizes_whitespace():
    html = "<p>Hello   \n\n  there,\t friend</p>"

    assert _html_to_text(html) == "Hello there, friend"


def test_html_to_text_empty_input_returns_empty_string():
    assert _html_to_text("") == ""
