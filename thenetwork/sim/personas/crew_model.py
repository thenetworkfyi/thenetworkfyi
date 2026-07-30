"""CrewAI LLM model adapter wrapper for simulation settings."""

from __future__ import annotations

from typing import Any

from crewai import LLM

from thenetwork.settings import Settings, get_settings


def _crewai_model_name(model: str) -> str:
    """Translate pydantic-ai's ``provider:model`` form for CrewAI."""
    provider, separator, model_name = model.partition(":")
    if separator and provider and model_name and "/" not in provider:
        return f"{provider}/{model_name}"
    return model


def build_crew_llm(
    model: str | None = None,
    api_key: str | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> LLM:
    """Build a CrewAI LLM instance using explicit arguments or simulation settings.

    If model or api_key are not explicitly passed, fallback values are retrieved from settings
    (or get_settings() if settings is None).
    """
    if settings is None:
        settings = get_settings()

    configured_model = model if model is not None else settings.agent_model
    effective_model = _crewai_model_name(configured_model)
    effective_api_key = api_key if api_key is not None else settings.agent_api_key

    llm_kwargs: dict[str, Any] = {
        "model": effective_model,
        "timeout": settings.model_request_timeout_seconds,
    }
    if effective_api_key:
        llm_kwargs["api_key"] = effective_api_key

    llm_kwargs.update(kwargs)
    return LLM(**llm_kwargs)
