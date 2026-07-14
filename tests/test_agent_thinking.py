from types import SimpleNamespace

from pydantic_ai.models.test import TestModel

from thenetwork.agent.core import build_agent
from thenetwork.settings import Settings


def test_agent_thinking_level_defaults_to_medium():
    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
    )

    assert settings.agent_thinking_level == "medium"


def test_agent_thinking_level_can_be_disabled():
    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
        agent_thinking_level=None,
    )

    assert settings.agent_thinking_level is None


def test_build_agent_applies_configured_thinking_level(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.agent.core.get_settings",
        lambda: SimpleNamespace(agent_model=TestModel(), agent_thinking_level="high"),
    )

    agent = build_agent()

    assert agent.model_settings == {"thinking": "high"}


def test_build_agent_omits_thinking_settings_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "thenetwork.agent.core.get_settings",
        lambda: SimpleNamespace(agent_model=TestModel(), agent_thinking_level=None),
    )

    agent = build_agent()

    assert agent.model_settings is None
