import json
import logging
from unittest.mock import patch

import httpx
import pytest
from pydantic_ai.models.test import TestModel

from thenetwork.audit import LOGGER_NAME, audit_run, audit_trace
from thenetwork.llm_observability import LLMWorkload, ObservedModel
from thenetwork.model_config import _TimedProviderClient, model_with_api_key


def _events(caplog) -> list[dict]:
    return [json.loads(record.message) for record in caplog.records if record.message]


def test_model_with_api_key_supplies_key_to_selected_provider():
    provider = object()
    provider_class = patch(
        "thenetwork.model_config.infer_provider_class",
        return_value=lambda *, api_key, http_client: (
            provider if api_key == "role-key" else None
        ),
    )
    infer_model = patch(
        "thenetwork.model_config.infer_model",
        return_value=object(),
    )

    with provider_class as mock_provider_class, infer_model as mock_infer_model:
        resolved = model_with_api_key("anthropic:claude-test", "role-key", 90.0)
        factory = mock_infer_model.call_args.kwargs["provider_factory"]
        assert factory("anthropic") is provider

    mock_provider_class.assert_called_once_with("anthropic")
    assert resolved is mock_infer_model.return_value


def test_model_with_api_key_preserves_concrete_test_model():
    model = TestModel()

    assert model_with_api_key(model, "unused", 90.0) is model


def test_model_with_api_key_wraps_the_requested_workload():
    resolved = model_with_api_key(
        TestModel(),
        "unused",
        90.0,
        workload=LLMWorkload.EMAIL_AGENT,
    )

    assert isinstance(resolved, ObservedModel)


@pytest.mark.asyncio
async def test_timed_provider_client_records_each_successful_attempt(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    async with _TimedProviderClient(transport=httpx.MockTransport(handler)) as client:
        with audit_run(), audit_trace("trace-1"):
            await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"x-stainless-retry-count": "1"},
            )

    event = _events(caplog)[0]
    assert event["event"] == "model.http_attempt.completed"
    assert event["timestamp"]
    assert event["trace_id"] == "trace-1"
    assert event["model_provider_host"] == "openrouter.ai"
    assert event["model_endpoint"] == "chat_completions"
    assert event["http_method"] == "POST"
    assert event["http_status"] == 200
    assert event["retry_count"] == 1
    assert event["outcome"] == "success"
    assert event["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_timed_provider_client_records_http_error_response(caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    async with _TimedProviderClient(transport=httpx.MockTransport(handler)) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"x-stainless-retry-count": "0"},
        )

    assert response.status_code == 429
    event = _events(caplog)[0]
    assert event["event"] == "model.http_attempt.completed"
    assert event["http_status"] == 429
    assert event["outcome"] == "error"


@pytest.mark.asyncio
async def test_timed_provider_client_records_timeout_attempt(caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with _TimedProviderClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout), audit_run():
            await client.post("https://api.openai.com/v1/embeddings")

    event = _events(caplog)[0]
    assert event["event"] == "model.http_attempt.completed"
    assert event["timestamp"]
    assert event["model_provider_host"] == "api.openai.com"
    assert event["model_endpoint"] == "embeddings"
    assert event["error_type"] == "ReadTimeout"
    assert event["outcome"] == "error"
    assert event["duration_ms"] >= 0
