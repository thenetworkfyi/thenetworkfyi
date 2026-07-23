import json
import logging
from decimal import Decimal
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from llama_index.embeddings.openai import OpenAIEmbedding
from pydantic_ai.messages import ModelResponse, RequestUsage, TextPart
from pydantic_ai.models import Model

from thenetwork.audit import LOGGER_NAME, audit_trace
from thenetwork.embed.embeddings import _ObservedOpenAIEmbedding
from thenetwork.llm_observability import (
    LLMWorkload,
    ObservedModel,
    observe_agent_duration,
    observe_email_lifecycle,
    record_llm_request,
)
from thenetwork.worker import metrics as worker_metrics


@pytest.fixture(autouse=True)
def _reset_model_metric_registry(monkeypatch):
    monkeypatch.setattr(worker_metrics, "_registered_llm_model_labels", {"unknown"})


def _events(caplog) -> list[dict]:
    return [json.loads(record.message) for record in caplog.records if record.message]


class _FakeModel(Model):
    def __init__(
        self,
        *,
        response: ModelResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self.response = response
        self.error = error

    @property
    def model_name(self) -> str:
        return "gpt-4.1-mini"

    @property
    def system(self) -> str:
        return "openai"

    async def request(self, messages, model_settings, model_request_parameters):
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _response(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content="content that must not enter accounting")],
        usage=RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        model_name="gpt-4.1-mini-2026-01-01",
        provider_name="openai",
    )


@pytest.mark.asyncio
async def test_observed_model_records_content_free_usage_cost_and_metrics(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    response = _response(
        input_tokens=100,
        output_tokens=25,
        cache_read_tokens=10,
        cache_write_tokens=5,
    )
    observed = ObservedModel(
        _FakeModel(response=response), workload=LLMWorkload.EMAIL_AGENT
    )

    with (
        patch.object(
            ModelResponse,
            "cost",
            return_value=SimpleNamespace(total_price=Decimal("0.0042")),
        ),
        patch("thenetwork.llm_observability.record_llm_request_metrics") as metrics,
        audit_trace("trace-1"),
    ):
        result = await observed.request([], None, None)  # type: ignore[arg-type]

    assert result is response
    event = next(
        item for item in _events(caplog) if item["event"] == "llm.request.completed"
    )
    assert event == {
        **event,
        "trace_id": "trace-1",
        "workload": "email_agent",
        "model_provider": "openai",
        "model_name": "gpt-4.1-mini-2026-01-01",
        "outcome": "success",
        "cost_status": "estimated",
        "estimated_cost_usd": 0.0042,
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
    }
    assert "content that must not enter accounting" not in json.dumps(event)
    metrics.assert_called_once()
    assert metrics.call_args.kwargs["model"] == "gpt-4.1-mini"
    assert metrics.call_args.kwargs["estimated_cost_usd"] == 0.0042


@pytest.mark.asyncio
async def test_observed_model_records_failure_without_error_text(caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    observed = ObservedModel(
        _FakeModel(error=RuntimeError("provider returned private content")),
        workload=LLMWorkload.MEMORY_SANITIZER,
    )

    with pytest.raises(RuntimeError), audit_trace("trace-2"):
        await observed.request([], None, None)  # type: ignore[arg-type]

    event = next(
        item for item in _events(caplog) if item["event"] == "llm.request.completed"
    )
    assert event["trace_id"] == "trace-2"
    assert event["workload"] == "memory_sanitizer"
    assert event["outcome"] == "error"
    assert event["error_type"] == "RuntimeError"
    assert event["cost_status"] == "unavailable"
    assert event["estimated_cost_usd"] is None
    assert event["input_tokens"] is None
    assert "provider returned private content" not in json.dumps(event)


def test_email_lifecycle_rolls_up_multiple_requests_and_cache_tokens(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    responses = [
        _response(input_tokens=100, output_tokens=10, cache_read_tokens=20),
        _response(input_tokens=50, output_tokens=5, cache_write_tokens=8),
    ]

    with (
        patch.object(
            ModelResponse,
            "cost",
            side_effect=[
                SimpleNamespace(total_price=Decimal("0.003")),
                SimpleNamespace(total_price=Decimal("0.002")),
            ],
        ),
        patch("thenetwork.llm_observability.record_email_lifecycle_metrics") as metrics,
        audit_trace("trace-rollup"),
        observe_email_lifecycle(time() - 1),
        observe_agent_duration(),
    ):
        for response in responses:
            record_llm_request(
                workload=LLMWorkload.EMAIL_AGENT,
                configured_model_name="gpt-4.1-mini",
                provider="openai",
                duration_ms=25,
                response=response,
            )

    event = next(
        item for item in _events(caplog) if item["event"] == "email.lifecycle.completed"
    )
    assert event["trace_id"] == "trace-rollup"
    assert event["model_request_count"] == 2
    assert event["usage_unavailable_request_count"] == 0
    assert event["unpriced_request_count"] == 0
    assert event["input_tokens"] == 150
    assert event["output_tokens"] == 15
    assert event["cache_read_tokens"] == 20
    assert event["cache_write_tokens"] == 8
    assert event["estimated_cost_usd"] == 0.005
    assert event["model_duration_ms"] == 50
    assert event["agent_observed"] is True
    assert event["total_duration_ms"] >= 1000
    assert event["queue_duration_ms"] >= 1000
    metrics.assert_called_once()
    assert metrics.call_args.kwargs["outcome"] == "success"
    assert metrics.call_args.kwargs["agent_duration_seconds"] is not None


def test_email_lifecycle_without_agent_skips_agent_histogram(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch("thenetwork.llm_observability.record_email_lifecycle_metrics") as metrics,
        audit_trace("trace-no-agent"),
        observe_email_lifecycle(time() - 1),
    ):
        pass

    event = next(
        item for item in _events(caplog) if item["event"] == "email.lifecycle.completed"
    )
    assert event["trace_id"] == "trace-no-agent"
    assert event["agent_observed"] is False
    assert event["agent_duration_ms"] == 0
    metrics.assert_called_once()
    assert metrics.call_args.kwargs["agent_duration_seconds"] is None


def test_unknown_price_is_explicit_and_not_added_as_zero(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with (
        patch.object(ModelResponse, "cost", side_effect=LookupError("unknown price")),
        patch("thenetwork.llm_observability.record_llm_request_metrics") as metrics,
    ):
        record_llm_request(
            workload=LLMWorkload.ABUSE_JUDGE,
            configured_model_name="unpriced-model",
            provider="openai",
            duration_ms=10,
            response=_response(input_tokens=10, output_tokens=2),
        )

    event = next(
        item for item in _events(caplog) if item["event"] == "llm.request.completed"
    )
    assert event["cost_status"] == "unavailable"
    assert event["estimated_cost_usd"] is None
    assert metrics.call_args.kwargs["estimated_cost_usd"] is None


@pytest.mark.asyncio
async def test_embedding_wrapper_accounts_for_one_llamaindex_api_batch():
    client = _ObservedOpenAIEmbedding(
        model="text-embedding-3-small",
        api_key="test-key",
    )
    vector = [0.1, 0.2]

    with (
        patch.object(
            OpenAIEmbedding,
            "_aget_text_embedding",
            new=AsyncMock(return_value=vector),
        ),
        patch("thenetwork.embed.embeddings.record_llm_request") as record,
    ):
        assert await client._aget_text_embedding("text to embed") == vector

    record.assert_called_once()
    fields = record.call_args.kwargs
    assert fields["workload"] is LLMWorkload.EMBEDDING
    assert fields["provider"] == "openai"
    assert fields["configured_model_name"] == "text-embedding-3-small"
    assert fields["response"].parts == []
    assert fields["response"].usage.input_tokens > 0
    assert "text to embed" not in repr(fields)


@pytest.mark.asyncio
async def test_process_email_rollup_matches_request_metric_totals(caplog):
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    session = MagicMock()
    session.get.return_value = None
    session.exec.return_value.first.return_value = "person-1"
    session_context = MagicMock()
    session_context.__enter__.return_value = session
    session_context.__exit__.return_value = False
    metric_calls: list[dict] = []

    async def fake_agent(**_kwargs):
        with observe_agent_duration():
            record_llm_request(
                workload=LLMWorkload.EMAIL_AGENT,
                configured_model_name="gpt-4.1-mini",
                provider="openai",
                duration_ms=100,
                response=_response(input_tokens=100, output_tokens=20),
            )
            embedding_usage = RequestUsage(input_tokens=30, output_tokens=0)
            record_llm_request(
                workload=LLMWorkload.EMBEDDING,
                configured_model_name="text-embedding-3-small",
                provider="openai",
                duration_ms=50,
                response=ModelResponse(
                    parts=[],
                    usage=embedding_usage,
                    model_name="text-embedding-3-small",
                    provider_name="openai",
                ),
            )

    with (
        patch.object(
            ModelResponse,
            "cost",
            side_effect=[
                SimpleNamespace(total_price=Decimal("0.004")),
                SimpleNamespace(total_price=Decimal("0.001")),
            ],
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=session_context),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=SimpleNamespace(
                sent_email_memories=[], handled=False, remainder=None, outcome=None
            ),
        ),
        patch(
            "thenetwork.worker.tasks.record_sent_email_memories",
            new=AsyncMock(),
        ),
        patch(
            "thenetwork.worker.tasks.run_agent_for_email",
            new=fake_agent,
        ),
        patch(
            "thenetwork.llm_observability.record_llm_request_metrics",
            side_effect=lambda **kwargs: metric_calls.append(kwargs),
        ),
    ):
        await process_email.func(
            sender_email="sender@example.com",
            subject="subject",
            body="body",
            sender_authenticated=True,
            trace_id="trace-process-email",
            intake_observed_at_epoch_seconds=time() - 1,
        )

    rollup = next(
        item for item in _events(caplog) if item["event"] == "email.lifecycle.completed"
    )
    assert rollup["trace_id"] == "trace-process-email"
    assert rollup["agent_observed"] is True
    assert rollup["model_request_count"] == len(metric_calls) == 2
    assert rollup["input_tokens"] == sum(
        item["input_tokens"] or 0 for item in metric_calls
    )
    assert rollup["output_tokens"] == sum(
        item["output_tokens"] or 0 for item in metric_calls
    )
    assert rollup["estimated_cost_usd"] == pytest.approx(
        sum(item["estimated_cost_usd"] or 0 for item in metric_calls)
    )
