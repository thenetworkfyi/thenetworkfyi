"""Model construction with workload-specific credentials."""

from __future__ import annotations

from time import monotonic
from typing import Any

import httpx
from pydantic_ai.models import infer_model
from pydantic_ai.providers import infer_provider_class

from thenetwork.audit import audit_event


class _TimedProviderClient(httpx.AsyncClient):
    """Emit one sealed audit event for every provider HTTP attempt.

    The OpenAI SDK retries beneath PydanticAI. Overriding ``send`` records every
    attempt, including a timeout that the SDK later retries successfully.
    """

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        started = monotonic()
        fields = {
            "model_provider_host": request.url.host,
            "model_endpoint": _model_endpoint(request.url.path),
            "http_method": request.method,
            "retry_count": _retry_count(request),
        }
        try:
            response = await super().send(request, **kwargs)
        except BaseException as exc:
            audit_event(
                "model.http_attempt.completed",
                **fields,
                outcome="error",
                error_type=type(exc).__name__,
                duration_ms=round((monotonic() - started) * 1000, 3),
            )
            raise
        audit_event(
            "model.http_attempt.completed",
            **fields,
            outcome="success",
            http_status=response.status_code,
            duration_ms=round((monotonic() - started) * 1000, 3),
        )
        return response


def _model_endpoint(path: str) -> str:
    if path.endswith("/chat/completions"):
        return "chat_completions"
    if path.endswith("/embeddings"):
        return "embeddings"
    if path.endswith("/responses"):
        return "responses"
    return "other"


def _retry_count(request: httpx.Request) -> int:
    try:
        return int(request.headers.get("x-stainless-retry-count", "0"))
    except ValueError:
        return 0


def model_with_api_key(model: Any, api_key: str, timeout: float) -> Any:
    """Resolve a configured model string using only the supplied API key.

    Every provider constructed here also gets an explicit httpx timeout
    (connect capped separately, same shape as the openai SDK's own default)
    instead of each library's own default of 600s, so a stalled upstream call
    fails into the caller's retry/error path rather than blocking a whole
    agent run. Concrete model instances used by tests and simulations are
    returned unchanged.
    """
    if not isinstance(model, str):
        return model

    http_timeout = httpx.Timeout(timeout, connect=5.0)

    def provider_factory(provider_name: str) -> Any:
        provider_class = infer_provider_class(provider_name)
        return provider_class(
            api_key=api_key, http_client=_TimedProviderClient(timeout=http_timeout)
        )

    return infer_model(model, provider_factory=provider_factory)
