"""Tests for inbound email body extraction (imap-tools + BeautifulSoup)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from thenetwork.email import inbound
from thenetwork.email.inbound import (
    MAX_BODY_CHARS,
    MAX_INLINE_LINKS,
    MAX_RENDERED_URL_CHARS,
    _html_to_text,
    _render_url,
)
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
    attachments: list | None = None,
):
    msg = MagicMock()
    msg.uid = uid
    msg.from_ = from_
    msg.subject = subject
    msg.headers = {}
    msg.text = text
    msg.html = html
    msg.attachments = attachments or []
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


def _attachment(
    *, filename: str = "", content_id: str = "", disposition: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        content_id=content_id,
        content_disposition=disposition,
    )


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


def test_render_url_preserves_short_http_url_verbatim():
    url = "https://example.com/projects/one?view=full#details"

    assert _render_url(url) == url


def test_render_url_truncates_path_tail_and_appends_ellipsis():
    url = f"https://example.com/{'useful-path/' * 20}?tracking=discarded#fragment"

    rendered = _render_url(url)

    assert len(rendered) == MAX_RENDERED_URL_CHARS
    assert rendered.startswith("https://example.com/useful-path/")
    assert rendered.endswith("…")
    assert "tracking" not in rendered
    assert "fragment" not in rendered


def test_render_url_drops_large_query_without_padding_to_budget():
    url = f"http://example.com/event?{'tracking=x&' * 30}"

    assert _render_url(url) == "http://example.com/event…"


def test_render_url_bounds_pathological_host():
    url = f"https://{'h' * 200}.example/path"

    rendered = _render_url(url)

    assert len(rendered) == MAX_RENDERED_URL_CHARS
    assert rendered.startswith("https://hhhh")
    assert rendered.endswith("…")
    assert "/path" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "data:text/plain;base64,SGVsbG8=",
        "javascript:alert(1)",
        "mailto:alice@example.com",
    ],
)
def test_render_url_rejects_non_http_schemes(url):
    assert _render_url(url) == ""


@pytest.mark.parametrize(
    ("attachments", "expected"),
    [
        (
            [_attachment(filename="logo.png", content_id="logo", disposition="inline")],
            0,
        ),
        ([_attachment(filename="brief.pdf", disposition="attachment")], 1),
        (
            [
                _attachment(filename="logo.png", content_id="logo"),
                _attachment(filename="brief.pdf", disposition="attachment"),
            ],
            1,
        ),
        ([], 0),
    ],
)
def test_count_stripped_attachments_excludes_inline_parts(attachments, expected):
    msg = _fake_message(attachments=attachments)

    assert inbound.count_stripped_attachments(msg) == expected


def test_poll_unseen_carries_attachment_count_on_oversize_rejection(
    fake_mailbox: _FakeMailBox,
):
    fake_mailbox.fetch.return_value = [
        _fake_message(
            text="a" * (inbound.MAX_RAW_BODY_CHARS + 1),
            attachments=[_attachment(filename="brief.pdf", disposition="attachment")],
        )
    ]

    [message] = inbound.poll_unseen()

    assert message.rejection_reason == inbound.REJECT_BODY_OVERSIZE
    assert message.attachment_count == 1


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


def test_html_to_text_preserves_hyperlink_referent():
    html = '<p>See my <a href="https://example.com/portfolio">portfolio</a>.</p>'

    assert _html_to_text(html) == (
        "See my portfolio (https://example.com/portfolio) ."
    )


def test_html_to_text_does_not_duplicate_auto_linked_url():
    url = "https://example.com/portfolio"

    assert _html_to_text(f'<a href="{url}">{url}</a>') == url


def test_html_to_text_deduplicates_identical_hrefs():
    url = "https://example.com/event"
    html = f'<a href="{url}">first</a> then <a href="{url}">second</a>'

    assert _html_to_text(html) == f"first ({url}) then second"


def test_html_to_text_caps_distinct_inlined_links():
    html = " ".join(
        f'<a href="https://example.com/{index}">link {index}</a>'
        for index in range(MAX_INLINE_LINKS + 5)
    )

    rendered = _html_to_text(html)

    assert rendered.count("(https://example.com/") == MAX_INLINE_LINKS
    assert f"link {MAX_INLINE_LINKS} (" not in rendered
    assert f"link {MAX_INLINE_LINKS + 4}" in rendered


def test_many_anchor_url_contribution_stays_below_body_cap():
    html = " ".join(
        f'<a href="https://example.com/{index}/{'p' * 200}">link {index}</a>'
        for index in range(100)
    )

    rendered = _html_to_text(html)

    assert rendered.count("…") == MAX_INLINE_LINKS
    assert len(rendered) < MAX_BODY_CHARS


def test_html_to_text_empty_input_returns_empty_string():
    assert _html_to_text("") == ""
