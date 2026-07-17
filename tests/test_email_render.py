"""Tests for the trusted email rendering boundary."""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup
from jinja2 import UndefinedError

from thenetwork.email.render import (
    FixedEmailTemplate,
    IntroductionEmailContext,
    QuotedMessage,
    SignatureVariant,
    _ENVIRONMENT,
    render_conversational_email,
    render_fixed_email,
)
from thenetwork.settings import Settings


def _visible_html(html: str) -> str:
    return "\n".join(
        line.strip()
        for line in BeautifulSoup(html, "html.parser").get_text("\n").splitlines()
        if line.strip()
    )


def test_conversational_renderer_escapes_injection_and_does_not_autolink():
    body = '<script>steal()</script> <img src=x onerror="steal()"> & "quotes" https://bad.example'

    rendered = render_conversational_email(body, html_enabled=True)

    assert rendered.html is not None
    assert "<script>steal()</script>" not in rendered.html
    assert '<img src=x onerror="steal()">' not in rendered.html
    assert "&lt;script&gt;steal()&lt;/script&gt;" in rendered.html
    assert "&lt;img src=x onerror=&#34;steal()&#34;&gt;" in rendered.html
    assert "https://bad.example" in rendered.html
    assert "<a " not in rendered.html
    assert BeautifulSoup(rendered.html, "html.parser").find("img") is None


def test_conversational_renderer_preserves_paragraphs_breaks_and_unicode():
    body = (
        'First & "quoted"\r\nsecond line\r\n\r\nMalformed <tag\n\r\nこんにちは, Łukasz'
    )

    rendered = render_conversational_email(
        body,
        signature_variant=SignatureVariant.NONE,
        html_enabled=True,
    )

    assert (
        rendered.text
        == 'First & "quoted"\nsecond line\n\nMalformed <tag\n\nこんにちは, Łukasz'
    )
    assert rendered.html is not None
    assert rendered.html.count("<p>") == 3
    assert rendered.html.count("<br>") == 1
    assert "&amp;" in rendered.html
    assert "&#34;quoted&#34;" in rendered.html
    assert "&lt;tag" in rendered.html
    assert "こんにちは, Łukasz" in rendered.html


def test_fixed_renderer_uses_only_named_template_and_escapes_context():
    rendered = render_fixed_email(
        FixedEmailTemplate.INTRODUCTION,
        IntroductionEmailContext(
            person_a_name='<img src=x onerror="steal()">',
            person_b_name="Renée & O'Connor",
        ),
        html_enabled=True,
    )

    assert rendered.html is not None
    assert "<img src=x" not in rendered.html
    assert "&lt;img src=x onerror=&#34;steal()&#34;&gt;" in rendered.html
    assert "Renée &amp; O&#39;Connor" in rendered.html
    with pytest.raises(TypeError, match="FixedEmailTemplate"):
        render_fixed_email("introduction", IntroductionEmailContext("A", "B"))  # type: ignore[arg-type]


def test_environment_is_strict_for_missing_fixed_context_fields():
    template = _ENVIRONMENT.get_template("fixed/introduction.html")

    with pytest.raises(UndefinedError):
        template.render(fixed_context={})


@pytest.mark.parametrize(
    ("variant", "expected_signature"),
    [
        (SignatureVariant.STANDARD, True),
        (SignatureVariant.STANDARD_WITH_REFERRAL, True),
        (SignatureVariant.NONE, False),
    ],
)
def test_signature_variants_are_rendered_once(variant, expected_signature):
    rendered = render_conversational_email(
        "A short note.",
        signature_variant=variant,
        referral_account="join@example.com",
        html_enabled=True,
    )

    assert rendered.html is not None
    assert (rendered.text.count("The Network") == 1) is expected_signature
    assert (rendered.html.count("The Network") == 1) is expected_signature
    if variant is SignatureVariant.STANDARD_WITH_REFERRAL:
        assert "join@example.com" in rendered.text
        assert "join@example.com" in rendered.html


def test_plain_and_html_have_equivalent_meaning_and_ordering():
    rendered = render_conversational_email(
        "First line\nsecond line\n\nSecond paragraph",
        quoted_message=QuotedMessage("Original line\nSecond original", "Tuesday"),
        html_enabled=True,
    )

    assert rendered.html is not None
    plain_visible = rendered.text.replace("\n--\n", "\n").replace("\n> ", "\n")
    html_visible = _visible_html(rendered.html)
    for text in (
        "First line",
        "second line",
        "Second paragraph",
        "The Network",
        "An automated connection service",
        "Reply anytime.",
    ):
        assert text in plain_visible
        assert text in html_visible
        assert plain_visible.index(text) < plain_visible.index("On Tuesday, you wrote:")
        assert html_visible.index(text) < html_visible.index("On Tuesday, you wrote:")
    assert (
        plain_visible.index("On Tuesday, you wrote:")
        < plain_visible.index("Original line")
        < plain_visible.index("Second original")
    )
    assert (
        html_visible.index("On Tuesday, you wrote:")
        < html_visible.index("Original line")
        < html_visible.index("Second original")
    )


def test_plain_only_fallback_is_explicit_and_complete():
    rendered = render_conversational_email("Body", html_enabled=False)

    assert rendered.html is None
    assert (
        rendered.text
        == "Body\n\n--\nThe Network\nAn automated connection service\nReply anytime."
    )


def test_html_feature_flag_defaults_to_plain_only():
    assert Settings.model_fields["html_email_enabled"].default is False


def test_html_document_is_accessible_fluid_and_has_no_remote_assets():
    rendered = render_conversational_email("Body", html_enabled=True)

    assert rendered.html is not None
    assert '<html lang="en" dir="ltr">' in rendered.html
    assert 'name="viewport"' in rendered.html
    assert "max-width: 600px" in rendered.html
    assert "font-size: 16px" in rendered.html
    assert "prefers-color-scheme: dark" in rendered.html
    assert not re.search(r"https?://|data:", rendered.html)
