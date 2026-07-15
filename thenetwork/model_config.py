"""Model construction with workload-specific credentials."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic_ai.models import infer_model
from pydantic_ai.providers import infer_provider_class


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
            api_key=api_key, http_client=httpx.AsyncClient(timeout=http_timeout)
        )

    return infer_model(model, provider_factory=provider_factory)
