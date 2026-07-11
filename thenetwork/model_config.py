"""Model construction with workload-specific credentials."""

from __future__ import annotations

from typing import Any

from pydantic_ai.models import infer_model
from pydantic_ai.providers import infer_provider_class


def model_with_api_key(model: Any, api_key: str) -> Any:
    """Resolve a configured model string using only the supplied API key.

    Concrete model instances used by tests and simulations are returned
    unchanged.
    """
    if not isinstance(model, str):
        return model

    def provider_factory(provider_name: str) -> Any:
        provider_class = infer_provider_class(provider_name)
        return provider_class(api_key=api_key)

    return infer_model(model, provider_factory=provider_factory)
