from __future__ import annotations

import pytest

from thenetwork.memory import sanitize as sanitize_mod
from thenetwork.security import log_redaction


def _span(label: str, start: int, end: int, score: float = 0.99) -> dict:
    return {"entity_group": label, "start": start, "end": end, "score": score}


def _use_spans(monkeypatch, spans_by_text: dict[str, list[dict]]) -> None:
    """Stand in for the local classifier with fixed per-string span lists.

    The redactor shares one loaded copy of the weights with the gist path, so
    it is stubbed the same way `tests/test_sanitize.py` stubs it: the model is
    a multi-gigabyte download, and what this repository owns is the label
    allow-list, the entity-type mapping, overlap coalescing, and keyed
    pseudonymization. Spans are shaped exactly like the pipeline's output.
    """
    monkeypatch.setattr(
        sanitize_mod,
        "_get_privacy_filter",
        lambda: lambda text: spans_by_text.get(text, []),
    )


def test_redacts_nested_strings_across_the_whole_taxonomy(monkeypatch):
    content = "Alice Chen called 415-555-0199 from https://example.test/a?x=1"
    identifier = "user_abcdef123456"
    _use_spans(
        monkeypatch,
        {
            content: [
                _span("private_person", 0, 10),
                _span("private_phone", 18, 30),
                _span("private_url", 36, 62),
            ],
            identifier: [_span("account_number", 0, 17)],
        },
    )
    raw = {"content": content, "calls": [{"id": identifier}]}

    redacted = log_redaction.redact_structured_log(raw, pseudonym_secret="test-key")

    assert redacted["content"].startswith(
        "[person] called [phone_number] from [url:log_v1_"
    )
    assert "example.test" not in redacted["content"]
    assert "415-555-0199" not in redacted["content"]
    assert "abcdef123456" not in redacted["calls"][0]["id"]
    assert redacted["calls"][0]["id"].startswith("[application_identifier:log_v1_")


def test_dates_are_redacted_here_even_though_gists_keep_them(monkeypatch):
    """The two allow-lists differ on purpose: a log has no recall requirement."""
    text = "met on March 3rd"
    _use_spans(monkeypatch, {text: [_span("private_date", 7, 16)]})

    assert log_redaction.redact_text(text) == "met on [date_time]"
    assert sanitize_mod.sanitize_text(text) == text


def test_stable_pseudonyms_are_keyed_and_do_not_fall_back_to_raw_values(monkeypatch):
    value = "user_abcdef123456"
    _use_spans(monkeypatch, {value: [_span("account_number", 0, 17)]})

    first = log_redaction.redact_text(value, pseudonym_secret="one")
    second = log_redaction.redact_text(value, pseudonym_secret="one")
    changed_key = log_redaction.redact_text(value, pseudonym_secret="two")
    unkeyed = log_redaction.redact_text(value)

    assert first == second
    assert first != changed_key
    assert "abcdef123456" not in first
    # A missing key must not degrade to an unkeyed hash, which candidate-value
    # lookup would reverse; it degrades to a non-correlatable placeholder.
    assert unkeyed == "[application_identifier]"


def test_adjacent_fragments_of_one_value_are_replaced_as_a_whole(monkeypatch):
    """The classifier labels tokens, so one value arrives split across spans."""
    text = "from mike_lay@example.test"
    _use_spans(
        monkeypatch,
        {text: [_span("private_email", 5, 13), _span("private_email", 13, 26)]},
    )

    redacted = log_redaction.redact_text(text)

    assert "mike_l" not in redacted
    assert "example.test" not in redacted
    assert redacted == "from [email_address]"


def test_overlapping_matches_never_leave_a_sensitive_fragment(monkeypatch):
    text = "contact alice.chen@example.test now"
    _use_spans(
        monkeypatch,
        {
            text: [
                _span("private_person", 8, 18),
                _span("private_email", 8, 31),
            ]
        },
    )

    redacted = log_redaction.redact_text(text, pseudonym_secret="key")

    assert "alice" not in redacted
    assert "example.test" not in redacted
    assert redacted == "contact [email_address] now"


def test_initialization_or_execution_failure_replaces_all_strings(monkeypatch):
    def unavailable():
        raise RuntimeError("the sanitizer model could not load")

    monkeypatch.setattr(sanitize_mod, "_get_privacy_filter", unavailable)
    raw = {"email": "alice@example.test", "nested": ["secret", {"x": "raw"}]}

    redacted = log_redaction.redact_structured_log(raw)

    assert redacted == {
        "[redacted-key-0]": "[redaction-unavailable]",
        "[redacted-key-1]": [
            "[redaction-unavailable]",
            {"[redacted-key-0]": "[redaction-unavailable]"},
        ],
    }


@pytest.mark.integration
@pytest.mark.real_sanitizer
@pytest.mark.parametrize(
    "text,expected_absent",
    [
        (
            "see https://internal.example.test/admin?token=abc123",
            ["internal.example.test"],
        ),
        ("sender was alice.chen+tag@example.test", ["alice.chen+tag@example.test"]),
        ("Alice Chen asked about the Rust meetup", ["Alice Chen"]),
        ("call 415-555-0199", ["415-555-0199"]),
    ],
)
def test_real_model_redacts_identifying_values_in_log_shaped_strings(
    text, expected_absent
):
    """Measured against openai/privacy-filter before the pattern tier was deleted."""
    redacted = log_redaction.redact_text(text, pseudonym_secret="test-key")
    for fragment in expected_absent:
        assert fragment not in redacted
