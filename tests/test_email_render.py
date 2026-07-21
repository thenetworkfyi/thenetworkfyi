"""Tests for the trusted email rendering boundary."""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup
from jinja2 import UndefinedError

from thenetwork.email.render import (
    ConsentRequestEmailContext,
    EmptyEmailContext,
    EventRecommendationEmailContext,
    EventRecommendationNotice,
    FirstContactWelcomeEmailContext,
    FixedEmailTemplate,
    InfrastructureRejectionEmailContext,
    InfrastructureRejectionReason,
    IntroductionEmailContext,
    QuotedMessage,
    SignatureVariant,
    _ENVIRONMENT,
    render_conversational_email,
    render_fixed_email,
)


def _visible_html(html: str) -> str:
    return "\n".join(
        line.strip()
        for line in BeautifulSoup(html, "html.parser").get_text("\n").splitlines()
        if line.strip()
    )


def test_conversational_renderer_escapes_injection_and_does_not_autolink():
    body = '<script>steal()</script> <img src=x onerror="steal()"> & "quotes" https://bad.example'

    rendered = render_conversational_email(body)

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
            relay_address="hidden+<script>@relay.example.com",
            person_a_gist='<img src=x onerror="steal()"> builds databases',
            person_b_gist="Works on Renée & O'Connor's storage project",
        ),
    )

    assert rendered.html is not None
    assert "<img src=x" not in rendered.html
    assert "&lt;img src=x onerror=&#34;steal()&#34;&gt;" in rendered.html
    assert "Renée &amp; O&#39;Connor" in rendered.html
    assert "hidden+&lt;script&gt;@relay.example.com" in rendered.html
    assert "Why you were matched:" in rendered.text
    assert "email hidden+<script>@relay.example.com directly" in rendered.text
    with pytest.raises(TypeError, match="FixedEmailTemplate"):
        render_fixed_email("introduction", IntroductionEmailContext("A", "B"))  # type: ignore[arg-type]


def test_introduction_renderer_omits_recap_for_legacy_context_without_gists():
    rendered = render_fixed_email(
        FixedEmailTemplate.INTRODUCTION,
        IntroductionEmailContext(relay_address="hidden-token@relay.example.com"),
        signature_variant=SignatureVariant.NONE,
    )

    assert "Why you were matched" not in rendered.text
    assert "email hidden-token@relay.example.com directly" in rendered.text


@pytest.mark.parametrize(
    ("template", "context", "expected_text"),
    [
        (
            FixedEmailTemplate.FIRST_CONTACT_WELCOME,
            FirstContactWelcomeEmailContext(),
            "To join, let us know something about yourself",
        ),
        (
            FixedEmailTemplate.INFRASTRUCTURE_REJECTION,
            InfrastructureRejectionEmailContext(
                InfrastructureRejectionReason.RATE_LIMIT
            ),
            "this address is sending too many messages right now",
        ),
    ],
)
def test_worker_fixed_templates_have_equivalent_plain_and_html_parts(
    template, context, expected_text
):
    rendered = render_fixed_email(
        template,
        context,
        signature_variant=SignatureVariant.NONE,
        quoted_message=QuotedMessage("Original line", "Tuesday"),
    )

    assert rendered.html is not None
    html_visible = _visible_html(rendered.html)
    assert expected_text in rendered.text
    assert expected_text in html_visible
    assert rendered.text.index(expected_text) < rendered.text.index("On Tuesday")
    assert html_visible.index(expected_text) < html_visible.index("On Tuesday")
    assert "Original line" in rendered.text
    assert "Original line" in html_visible


def test_fixed_renderer_rejects_mismatched_or_untrusted_worker_contexts():
    with pytest.raises(TypeError, match="first_contact_welcome"):
        render_fixed_email(
            FixedEmailTemplate.FIRST_CONTACT_WELCOME,
            IntroductionEmailContext("Alice", "Bob"),
        )
    with pytest.raises(TypeError, match="InfrastructureRejectionReason"):
        render_fixed_email(
            FixedEmailTemplate.INFRASTRUCTURE_REJECTION,
            InfrastructureRejectionEmailContext("<script>steal()</script>"),  # type: ignore[arg-type]
        )


def test_consent_request_renderer_preserves_anonymous_plain_body_and_escapes_context():
    rendered = render_fixed_email(
        FixedEmailTemplate.CONSENT_REQUEST,
        ConsentRequestEmailContext(
            counterpart_gist='<img src=x onerror="steal()"> builds databases',
            reply_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa<script>",
        ),
        signature_variant=SignatureVariant.NONE,
    )

    assert (
        rendered.text == "A possible match came up:\n\n"
        '<img src=x onerror="steal()"> builds databases\n\n'
        "No name or contact details have been shared. Reply YES to opt in, or NO "
        "to decline. If you reply from another thread, include this token in your "
        "reply: [intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa<script>]"
    )
    assert rendered.html is not None
    assert '<img src=x onerror="steal()">' not in rendered.html
    assert "&lt;img src=x onerror=&#34;steal()&#34;&gt;" in rendered.html
    assert "&lt;script&gt;" in rendered.html
    assert "No name or contact details have been shared." in _visible_html(
        rendered.html
    )


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        (
            FixedEmailTemplate.CONSENT_CLARIFICATION,
            "I could not determine your response. Reply with YES to opt in, NO to "
            "decline, or REVOKE to withdraw consent.",
        ),
        (
            FixedEmailTemplate.CONSENT_ACKNOWLEDGMENT,
            "Noted — waiting on the other party.",
        ),
        (
            FixedEmailTemplate.CONSENT_DECLINED,
            "Noted — this introduction will not proceed.",
        ),
        (
            FixedEmailTemplate.CONSENT_ALREADY_DECLINED,
            "This introduction has already been declined and will not proceed.",
        ),
    ],
)
def test_fixed_consent_replies_have_equivalent_plain_and_html_meaning(
    template, expected
):
    rendered = render_fixed_email(
        template,
        EmptyEmailContext(),
        signature_variant=SignatureVariant.NONE,
    )

    assert rendered.text == expected
    assert rendered.html is not None
    assert _visible_html(rendered.html) == expected


def test_fixed_renderer_rejects_mismatched_context():
    with pytest.raises(TypeError, match="ConsentRequestEmailContext"):
        render_fixed_email(
            FixedEmailTemplate.CONSENT_REQUEST,
            EmptyEmailContext(),
        )


def test_environment_is_strict_for_missing_fixed_context_fields():
    template = _ENVIRONMENT.get_template("fixed/introduction.html")

    with pytest.raises(UndefinedError):
        template.render(fixed_context={})


def test_event_recommendation_template_escapes_only_the_sealed_gist():
    rendered = render_fixed_email(
        FixedEmailTemplate.EVENT_RECOMMENDATION,
        EventRecommendationEmailContext(
            event_gist='<img src=x onerror="steal()">',
            notice=EventRecommendationNotice.FIRST,
        ),
        signature_variant=SignatureVariant.NONE,
    )

    assert rendered.html is not None
    assert "<img src=x" not in rendered.html
    assert "&lt;img src=x onerror=&#34;steal()&#34;&gt;" in rendered.html
    assert EventRecommendationNotice.FIRST.value in rendered.text
    with pytest.raises(TypeError, match="EventRecommendationEmailContext"):
        render_fixed_email(
            FixedEmailTemplate.EVENT_RECOMMENDATION,
            IntroductionEmailContext("A", "B"),
        )


@pytest.mark.parametrize(
    ("variant", "expected_signature"),
    [
        (SignatureVariant.STANDARD, True),
        (SignatureVariant.NONE, False),
    ],
)
def test_signature_variants_are_rendered_once(variant, expected_signature):
    rendered = render_conversational_email(
        "A short note.",
        signature_variant=variant,
    )

    assert rendered.html is not None
    assert (rendered.text.count("The Network") == 1) is expected_signature
    assert (rendered.html.count("The Network") == 1) is expected_signature
    assert (rendered.text.count("join@thenetwork.fyi") == 1) is expected_signature
    assert (rendered.html.count("join@thenetwork.fyi") == 1) is expected_signature


def test_plain_and_html_have_equivalent_meaning_and_ordering():
    rendered = render_conversational_email(
        "First line\nsecond line\n\nSecond paragraph",
        quoted_message=QuotedMessage("Original line\nSecond original", "Tuesday"),
    )

    assert rendered.html is not None
    plain_visible = rendered.text.replace("\n> ", "\n")
    html_visible = _visible_html(rendered.html)
    for text in (
        "First line",
        "second line",
        "Second paragraph",
        "The Network",
        "join@thenetwork.fyi",
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


def test_html_document_is_accessible_fluid_and_has_no_remote_assets():
    rendered = render_conversational_email("Body")

    assert rendered.html is not None
    assert '<html lang="en" dir="ltr">' in rendered.html
    assert 'name="viewport"' in rendered.html
    assert "max-width: 600px" in rendered.html
    assert "font-size: 16px" in rendered.html
    assert "prefers-color-scheme: dark" in rendered.html
    assert not re.search(r"https?://|data:", rendered.html)
