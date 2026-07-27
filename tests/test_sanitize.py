from __future__ import annotations

import pytest

from thenetwork.db.models import Memory
from thenetwork.memory import sanitize as sanitize_mod


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Memory] = []
        self.flushes = 0

    def add(self, memory: Memory) -> None:
        self.added.append(memory)

    def flush(self) -> None:
        self.flushes += 1


def _span(label: str, start: int, end: int, score: float = 0.99) -> dict:
    return {"entity_group": label, "start": start, "end": end, "score": score}


def _use_spans(monkeypatch, spans: list[dict]) -> None:
    """Stand in for the local classifier with a fixed span list.

    The real model is a multi-gigabyte download, so CI exercises the parts
    this repository owns - the label allow-list, span merging, and
    right-to-left substitution - against spans shaped exactly like the
    pipeline's output. The model's own accuracy is covered by the
    integration test below.
    """
    monkeypatch.setattr(sanitize_mod, "_get_privacy_filter", lambda: lambda text: spans)


def test_sanitize_memory_redacts_names_emails_and_phones(monkeypatch):
    text = "Alice Smith emailed alice@example.com from 415-555-0199"
    _use_spans(
        monkeypatch,
        [
            _span("private_person", 0, 11),
            _span("private_email", 20, 37),
            _span("private_phone", 43, 55),
        ],
    )
    memory = Memory(text=text, refs=["person-1"])
    session = FakeSession()

    gist = sanitize_mod.sanitize_memory(memory, session)

    assert gist == "[name] emailed [email] from [phone]"
    assert memory.gist == gist
    assert session.flushes == 1


def test_sanitize_memory_rejects_a_memory_with_no_refs(monkeypatch):
    _use_spans(monkeypatch, [])
    with pytest.raises(ValueError, match="no refs"):
        sanitize_mod.sanitize_memory(
            Memory(text="general note", refs=[]), FakeSession()
        )


def test_fragmented_spans_are_merged_into_one_token(monkeypatch):
    """The model labels tokens, so one value arrives in pieces.

    'mike_lay' comes back as ' mike_l' + 'ay'. Substituting each fragment
    would emit '[name][name]', and a fragment that fell outside the
    allow-list would leave part of the name in the gist.
    """
    text = "ping mike_lay today"
    _use_spans(
        monkeypatch,
        [_span("private_person", 4, 11), _span("private_person", 11, 13)],
    )

    assert sanitize_mod.sanitize_text(text) == "ping [name] today"


def test_leading_whitespace_is_kept_outside_the_replacement(monkeypatch):
    """The pipeline folds the preceding space into a span.

    Splicing that span verbatim would delete the separator and run the token
    into the previous word ('reach[email]').
    """
    text = "reach alice@example.com"
    _use_spans(monkeypatch, [_span("private_email", 5, 23)])

    assert sanitize_mod.sanitize_text(text) == "reach [email]"


def test_adjacent_spans_with_different_labels_are_not_merged(monkeypatch):
    text = "Alice alice@example.com"
    _use_spans(
        monkeypatch,
        [_span("private_person", 0, 5), _span("private_email", 5, 23)],
    )

    assert sanitize_mod.sanitize_text(text) == "[name] [email]"


def test_dates_survive_because_they_are_recall_and_perishability_signal(monkeypatch):
    """'a Rust meetup Thursday' is what the gist exists to embed."""
    text = "a Rust meetup Thursday"
    _use_spans(monkeypatch, [_span("private_date", 14, 22)])

    assert sanitize_mod.sanitize_text(text) == text


def test_urls_are_redacted_because_a_profile_url_is_a_handle(monkeypatch):
    text = "profile https://github.com/mkly here"
    _use_spans(monkeypatch, [_span("private_url", 8, 31)])

    assert sanitize_mod.sanitize_text(text) == "profile [url] here"


def test_unlisted_labels_are_left_alone(monkeypatch):
    text = "some text"
    _use_spans(monkeypatch, [_span("not_a_real_label", 0, 4)])

    assert sanitize_mod.sanitize_text(text) == text


def test_blank_text_skips_the_model_entirely(monkeypatch):
    def explode() -> None:
        raise AssertionError("the classifier must not be called for blank text")

    monkeypatch.setattr(sanitize_mod, "_get_privacy_filter", explode)

    assert sanitize_mod.sanitize_text("   ") == "   "


def test_sanitizer_fails_loud_when_transformers_is_unavailable(monkeypatch):
    """A missing sanitizer is a deployment error, never a silent downgrade.

    Without it there is no redaction at all, so a cross-user gist would
    carry raw names.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "transformers":
            raise ImportError("no transformers")
        return real_import(name, *args, **kwargs)

    sanitize_mod._get_privacy_filter.cache_clear()
    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(RuntimeError, match="transformers is required"):
        sanitize_mod.assert_sanitizer_ready()

    sanitize_mod._get_privacy_filter.cache_clear()


@pytest.mark.integration
@pytest.mark.real_sanitizer
@pytest.mark.parametrize(
    "text,expected_absent",
    [
        ("Alice Smith emailed alice@example.com", ["Alice Smith", "alice@example.com"]),
        ("my github username is mkly", ["mkly"]),
        ("follow @atlas for updates", ["atlas"]),
        ("Rose introduced Mark to Bill", ["Rose", "Mark", "Bill"]),
        ("reach me at mike [at] mkly [dot] io", ["mkly"]),
    ],
)
def test_real_model_removes_identifying_text(text, expected_absent):
    """Cases verified against openai/privacy-filter before it was adopted."""
    gist = sanitize_mod.sanitize_text(text)
    for fragment in expected_absent:
        assert fragment not in gist


@pytest.mark.integration
@pytest.mark.real_sanitizer
@pytest.mark.parametrize(
    "text",
    [
        "LinkedIn: Senior Engineer",
        "Slack: mostly async",
        "she works at Kestrel Biolabs in Tromso, Norway",
        "just moved to Berlin, looking for a Rust cofounder",
    ],
)
def test_real_model_keeps_role_company_and_place_recall_text(text):
    """Organizations and places stay: these gists are embedded for that recall.

    The deleted pattern tier wrongly tagged 'LinkedIn' as a person name.
    """
    assert sanitize_mod.sanitize_text(text) == text
