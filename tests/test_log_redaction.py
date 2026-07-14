from types import SimpleNamespace

from thenetwork.security import log_redaction


class FakeAnalyzer:
    def __init__(self, results=()):
        self.results = results
        self.calls = []

    def analyze(self, *, text, language):
        self.calls.append((text, language))
        return self.results


def test_redacts_nested_strings_broad_pii_and_custom_sensitive_values(monkeypatch):
    analyzer = FakeAnalyzer([SimpleNamespace(start=0, end=10, entity_type="PERSON")])
    monkeypatch.setattr(log_redaction, "_get_log_analyzer", lambda: analyzer)
    raw = {
        "content": "Alice Chen used https://example.test/a?x=1 with api_key=sk_abcdefghijklmnopq",
        "calls": [
            {"token": "[intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"},
            {"id": "request_abcdef123456"},
        ],
    }

    redacted = log_redaction.redact_structured_log(raw, pseudonym_secret="test-key")

    assert redacted["content"].startswith("[person] used [url:log_v1_")
    assert "example.test" not in redacted["content"]
    assert "sk_abcdefghijklmnopq" not in redacted["content"]
    assert "aaaaaaaa-aaaa" not in redacted["calls"][0]["token"]
    assert "request_abcdef123456" not in redacted["calls"][1]["id"]
    assert analyzer.calls


def test_stable_pseudonyms_are_keyed_and_do_not_fall_back_to_raw_values(monkeypatch):
    monkeypatch.setattr(log_redaction, "_get_log_analyzer", lambda: FakeAnalyzer())
    value = "[intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"

    first = log_redaction.redact_text(value, pseudonym_secret="one")
    second = log_redaction.redact_text(value, pseudonym_secret="one")
    changed_key = log_redaction.redact_text(value, pseudonym_secret="two")
    unkeyed = log_redaction.redact_text(value)

    assert first == second
    assert first != changed_key
    assert "aaaaaaaa" not in first
    assert unkeyed == "[intro_token]"


def test_overlapping_matches_never_leave_a_sensitive_fragment(monkeypatch):
    token = "[intro:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa]"
    analyzer = FakeAnalyzer(
        [SimpleNamespace(start=7, end=43, entity_type="APPLICATION_IDENTIFIER")]
    )
    monkeypatch.setattr(log_redaction, "_get_log_analyzer", lambda: analyzer)

    redacted = log_redaction.redact_text(f"token: {token}", pseudonym_secret="key")

    assert "aaaaaaaa" not in redacted
    assert redacted.startswith("token: [intro_token:log_v1_")


def test_initialization_or_execution_failure_replaces_all_strings(monkeypatch):
    def unavailable():
        raise log_redaction.LogRedactionError("no model")

    monkeypatch.setattr(log_redaction, "_get_log_analyzer", unavailable)
    raw = {"email": "alice@example.test", "nested": ["secret", {"x": "raw"}]}

    redacted = log_redaction.redact_structured_log(raw)

    assert redacted == {
        "[redacted-key-0]": "[redaction-unavailable]",
        "[redacted-key-1]": [
            "[redaction-unavailable]",
            {"[redacted-key-0]": "[redaction-unavailable]"},
        ],
    }
