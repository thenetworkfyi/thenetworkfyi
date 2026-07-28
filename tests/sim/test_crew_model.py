"""Unit tests for CrewAI LLM model adapter."""

from thenetwork.settings import Settings
from thenetwork.sim.personas.crew_model import build_crew_llm


def test_build_crew_llm_defaults():
    custom_settings = Settings(
        agent_model="openai/gpt-4o",
        small_agent_model="openai/gpt-4o-mini",
        embed_model="openai/text-embedding-3-small",
        relay_domain="example.test",
        agent_api_key="default-api-key",
        model_request_timeout_seconds=45.0,
    )
    llm = build_crew_llm(settings=custom_settings)

    assert llm.model == "gpt-4o"
    assert llm.provider == "openai"
    assert llm.api_key == "default-api-key"
    assert llm.timeout == 45.0


def test_build_crew_llm_explicit_overrides():
    custom_settings = Settings(
        agent_model="openai/gpt-4o",
        small_agent_model="openai/gpt-4o-mini",
        embed_model="openai/text-embedding-3-small",
        relay_domain="example.test",
        agent_api_key="default-api-key",
        model_request_timeout_seconds=30.0,
    )
    llm = build_crew_llm(
        model="anthropic/claude-3-5-sonnet",
        api_key="override-key",
        settings=custom_settings,
        temperature=0.5,
    )

    assert llm.model == "claude-3-5-sonnet"
    assert llm.provider == "anthropic"
    assert llm.api_key == "override-key"
    assert llm.timeout == 30.0
    assert llm.temperature == 0.5


def test_build_crew_llm_without_api_key():
    custom_settings = Settings(
        agent_model="ollama/llama3",
        small_agent_model="ollama/llama3",
        embed_model="openai/text-embedding-3-small",
        relay_domain="example.test",
        agent_api_key="",
        model_request_timeout_seconds=60.0,
    )
    llm = build_crew_llm(settings=custom_settings)

    assert llm.model == "llama3"
    assert llm.provider == "ollama"
    assert llm.api_key in (None, "", "ollama")
    assert llm.timeout == 60.0
