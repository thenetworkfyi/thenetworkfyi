"""Content-free accounting for model, embedding, and email lifecycle work."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from time import monotonic, time
from typing import Iterator

from pydantic_ai.messages import ModelResponse, RequestUsage
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from thenetwork.audit import audit_event
from thenetwork.worker.metrics import (
    record_email_lifecycle_metrics,
    record_llm_request_metrics,
    register_llm_model_label,
)


class LLMWorkload(StrEnum):
    EMAIL_AGENT = "email_agent"
    MEMORY_SANITIZER = "memory_sanitizer"
    ABUSE_JUDGE = "abuse_judge"
    EMBEDDING = "embedding"


class CostStatus(StrEnum):
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


_KNOWN_PROVIDERS = frozenset(
    {
        "anthropic",
        "bedrock",
        "cerebras",
        "cohere",
        "fireworks",
        "google-gla",
        "google-vertex",
        "groq",
        "huggingface",
        "mistral",
        "ollama",
        "openai",
        "openrouter",
        "test",
        "xai",
    }
)
_SAFE_MODEL_LABEL = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


@dataclass(slots=True)
class _LifecycleTotals:
    model_request_count: int = 0
    usage_unavailable_request_count: int = 0
    unpriced_request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    model_duration_ms: float = 0.0
    agent_duration_ms: float = 0.0


_lifecycle_totals: ContextVar[_LifecycleTotals | None] = ContextVar(
    "thenetwork_llm_lifecycle_totals",
    default=None,
)


def _provider_label(provider: object) -> str:
    candidate = str(provider or "").strip().lower()
    return candidate if candidate in _KNOWN_PROVIDERS else "other"


def _model_label(model_name: object) -> str:
    candidate = str(model_name or "").strip()
    return candidate if _SAFE_MODEL_LABEL.fullmatch(candidate) else "unknown"


def _usage_value(usage: RequestUsage | None, field: str) -> int | None:
    if usage is None:
        return None
    value = getattr(usage, field, None)
    return int(value) if value is not None else 0


def _estimated_cost(response: ModelResponse | None) -> tuple[float | None, CostStatus]:
    if response is None:
        return None, CostStatus.UNAVAILABLE
    try:
        return float(response.cost().total_price), CostStatus.ESTIMATED
    except Exception:
        return None, CostStatus.UNAVAILABLE


def record_llm_request(
    *,
    workload: LLMWorkload,
    configured_model_name: object,
    provider: object,
    duration_ms: float,
    response: ModelResponse | None = None,
    usage: RequestUsage | None = None,
    error_type: str | None = None,
) -> None:
    """Record one logical request without prompts, responses, or tool arguments."""
    resolved_usage = response.usage if response is not None else usage
    outcome = "error" if error_type is not None else "success"
    provider_label = _provider_label(
        response.provider_name if response is not None else provider
    )
    metric_model_label = _model_label(configured_model_name)
    metric_model_label = register_llm_model_label(metric_model_label)
    audit_model_label = _model_label(
        response.model_name if response is not None else configured_model_name
    )
    estimated_cost_usd, cost_status = _estimated_cost(response)
    token_fields = {
        "input_tokens": _usage_value(resolved_usage, "input_tokens"),
        "output_tokens": _usage_value(resolved_usage, "output_tokens"),
        "cache_read_tokens": _usage_value(resolved_usage, "cache_read_tokens"),
        "cache_write_tokens": _usage_value(resolved_usage, "cache_write_tokens"),
    }

    totals = _lifecycle_totals.get()
    if totals is not None:
        totals.model_request_count += 1
        totals.model_duration_ms += max(0.0, duration_ms)
        if resolved_usage is None:
            totals.usage_unavailable_request_count += 1
        else:
            totals.input_tokens += token_fields["input_tokens"] or 0
            totals.output_tokens += token_fields["output_tokens"] or 0
            totals.cache_read_tokens += token_fields["cache_read_tokens"] or 0
            totals.cache_write_tokens += token_fields["cache_write_tokens"] or 0
        if estimated_cost_usd is None:
            totals.unpriced_request_count += 1
        else:
            totals.estimated_cost_usd += Decimal(str(estimated_cost_usd))

    fields: dict[str, object] = {
        "workload": workload.value,
        "model_provider": provider_label,
        "model_name": audit_model_label,
        "outcome": outcome,
        "duration_ms": round(max(0.0, duration_ms), 3),
        "cost_status": cost_status.value,
        "estimated_cost_usd": estimated_cost_usd,
        **token_fields,
    }
    if error_type is not None:
        fields["error_type"] = error_type
    try:
        audit_event("llm.request.completed", **fields)
    except Exception:
        # Telemetry must not alter email processing or model retry behavior.
        pass

    record_llm_request_metrics(
        workload=workload.value,
        provider=provider_label,
        model=metric_model_label,
        outcome=outcome,
        cost_status=cost_status.value,
        duration_seconds=max(0.0, duration_ms) / 1000,
        input_tokens=token_fields["input_tokens"],
        output_tokens=token_fields["output_tokens"],
        cache_read_tokens=token_fields["cache_read_tokens"],
        cache_write_tokens=token_fields["cache_write_tokens"],
        estimated_cost_usd=estimated_cost_usd,
    )


class ObservedModel(WrapperModel):
    """Wrap a Pydantic AI model at the logical request boundary."""

    def __init__(self, wrapped: Model, *, workload: LLMWorkload) -> None:
        super().__init__(wrapped)
        self._workload = workload
        self._configured_model_name = _model_label(self.wrapped.model_name)
        self._configured_provider = _provider_label(self.wrapped.system)
        register_llm_model_label(self._configured_model_name)

    async def request(
        self,
        messages,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        started = monotonic()
        try:
            response = await self.wrapped.request(
                messages,
                model_settings,
                model_request_parameters,
            )
        except BaseException as exc:
            record_llm_request(
                workload=self._workload,
                configured_model_name=self._configured_model_name,
                provider=self._configured_provider,
                duration_ms=(monotonic() - started) * 1000,
                error_type=type(exc).__name__,
            )
            raise
        record_llm_request(
            workload=self._workload,
            configured_model_name=self._configured_model_name,
            provider=self._configured_provider,
            duration_ms=(monotonic() - started) * 1000,
            response=response,
        )
        return response


def observe_model(model: Model, *, workload: LLMWorkload) -> Model:
    if isinstance(model, ObservedModel):
        return model
    return ObservedModel(model, workload=workload)


@contextmanager
def observe_agent_duration() -> Iterator[None]:
    started = monotonic()
    try:
        yield
    finally:
        totals = _lifecycle_totals.get()
        if totals is not None:
            totals.agent_duration_ms += max(0.0, (monotonic() - started) * 1000)


@contextmanager
def observe_email_lifecycle(
    intake_observed_at_epoch_seconds: float | None,
) -> Iterator[None]:
    """Roll up all observed model work performed during one task attempt."""
    totals = _LifecycleTotals()
    token = _lifecycle_totals.set(totals)
    process_started = monotonic()
    task_started_at = time()
    queue_duration_ms = (
        max(0.0, (task_started_at - intake_observed_at_epoch_seconds) * 1000)
        if intake_observed_at_epoch_seconds is not None
        else None
    )
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        process_duration_ms = max(0.0, (monotonic() - process_started) * 1000)
        total_duration_ms = (
            max(0.0, (time() - intake_observed_at_epoch_seconds) * 1000)
            if intake_observed_at_epoch_seconds is not None
            else None
        )
        fields: dict[str, object] = {
            "outcome": outcome,
            "intake_observed": intake_observed_at_epoch_seconds is not None,
            "process_duration_ms": round(process_duration_ms, 3),
            "total_duration_ms": (
                round(total_duration_ms, 3) if total_duration_ms is not None else None
            ),
            "queue_duration_ms": (
                round(queue_duration_ms, 3) if queue_duration_ms is not None else None
            ),
            "agent_duration_ms": round(totals.agent_duration_ms, 3),
            "model_duration_ms": round(totals.model_duration_ms, 3),
            "model_request_count": totals.model_request_count,
            "usage_unavailable_request_count": totals.usage_unavailable_request_count,
            "unpriced_request_count": totals.unpriced_request_count,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_tokens": totals.cache_read_tokens,
            "cache_write_tokens": totals.cache_write_tokens,
            "estimated_cost_usd": float(totals.estimated_cost_usd),
        }
        try:
            audit_event("email.lifecycle.completed", **fields)
        except Exception:
            pass
        record_email_lifecycle_metrics(
            outcome=outcome,
            total_duration_seconds=(
                total_duration_ms / 1000 if total_duration_ms is not None else None
            ),
            queue_duration_seconds=(
                queue_duration_ms / 1000 if queue_duration_ms is not None else None
            ),
            agent_duration_seconds=totals.agent_duration_ms / 1000,
        )
        _lifecycle_totals.reset(token)
