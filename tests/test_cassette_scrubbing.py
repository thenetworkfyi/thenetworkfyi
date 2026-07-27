"""Offline checks for live-model cassette sanitization."""

import json

from types import SimpleNamespace

from vcr.request import Request

from tests.conftest import (
    discard_cassette_on_test_failure,
    scrub_cassette_request,
    scrub_cassette_response,
)


def test_cassette_response_scrubs_provider_identifiers_and_headers():
    response = {
        "headers": {
            "content-type": ["application/json"],
            "x-request-id": ["req-secret"],
            "set-cookie": ["provider-session=secret"],
        },
        "body": {
            "string": json.dumps(
                {
                    "id": "generation-secret",
                    "request_id": "request-secret",
                    "choices": [{"message": {"content": "safe output"}}],
                    "usage": {"id": "nested-provider-secret"},
                }
            ).encode()
        },
    }

    scrubbed = scrub_cassette_response(response)
    payload = json.loads(scrubbed["body"]["string"])

    assert scrubbed["headers"] == {"content-type": ["application/json"]}
    assert payload["id"] == "[REDACTED_PROVIDER_ID]"
    assert payload["request_id"] == "[REDACTED_PROVIDER_ID]"
    assert payload["usage"]["id"] == "[REDACTED_PROVIDER_ID]"
    assert payload["choices"][0]["message"]["content"] == "safe output"


def test_cassette_response_discards_provider_errors():
    response = {
        "status": {"code": 429, "message": "Too Many Requests"},
        "headers": {},
        "body": {"string": b'{"error":"rate limited"}'},
    }

    assert scrub_cassette_response(response) is None


def test_cassette_request_scrubs_echoed_provider_tool_ids():
    request = Request(
        "POST",
        "https://provider.example/chat/completions",
        json.dumps(
            {
                "messages": [
                    {
                        "role": "tool",
                        "tool_call_id": "chatcmpl-tool-secret",
                    }
                ]
            }
        ),
        {},
    )

    scrubbed = scrub_cassette_request(request)
    payload = json.loads(scrubbed.body)

    assert payload["messages"][0]["tool_call_id"] == "[REDACTED_PROVIDER_ID]"


def test_failed_test_discards_dirty_cassette(tmp_path):
    cassette_path = tmp_path / "failed-case.yaml"
    cassette_path.write_text("partial recording")
    cassette = SimpleNamespace(dirty=True, _path=cassette_path)

    discard_cassette_on_test_failure(cassette, SimpleNamespace(passed=False))

    assert cassette.dirty is False
    assert not cassette_path.exists()


def test_successful_test_keeps_dirty_cassette_for_vcr_to_save():
    cassette = SimpleNamespace(dirty=True)

    discard_cassette_on_test_failure(cassette, SimpleNamespace(passed=True))

    assert cassette.dirty is True


def test_interrupted_test_discards_cassette_without_a_call_report(tmp_path):
    cassette_path = tmp_path / "interrupted-case.yaml"
    cassette_path.write_text("partial recording")
    cassette = SimpleNamespace(dirty=True, _path=cassette_path)

    discard_cassette_on_test_failure(cassette, None)

    assert cassette.dirty is False
    assert not cassette_path.exists()
