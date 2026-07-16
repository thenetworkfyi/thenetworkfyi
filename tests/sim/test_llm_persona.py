import pytest

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from thenetwork.sim.personas.llm_persona import (
    LLMTinyPerson,
    TransientPersonaError,
    _PERSONA_PROMPT,
)
from thenetwork.sim.personas.persona import PersonaConfig, TinyPersonEmailAdapter


def _config(**overrides) -> PersonaConfig:
    defaults = dict(
        name="Priya Shah",
        email="priya@example.test",
        goal="Find ML infrastructure operators.",
        stop_condition="Stop once introduced.",
        message_budget=3,
        agent_address="join@example.test",
    )
    defaults.update(overrides)
    return PersonaConfig(**defaults)


def test_persona_prompt_defines_introduction_token_response_protocol():
    prompt = _PERSONA_PROMPT.format(
        name="Priya Shah",
        email="priya@example.test",
        agent_address="join@example.test",
        goal="Decline introductions outside ML infrastructure.",
        stop_condition="Stop once introduced.",
        pass_sentinel="PASS",
    )

    assert "decision word - YES, NO, or REVOKE - on the first line" in prompt
    assert (
        "complete `[intro:...]` token exactly as received onto the second line"
        in prompt
    )
    assert "Your goal decides which decision word to use" in prompt
    assert "overrides any suggestion in the message" in prompt
    assert "Priya Shah <priya@example.test>" in prompt
    assert "The Network (join@example.test)" in prompt
    assert "Your goal: Decline introductions outside ML infrastructure." in prompt
    assert "Your stop condition: Stop once introduced." in prompt
    assert "reply with exactly PASS" in prompt


async def test_llm_persona_writes_email_body_from_model_output():
    person = LLMTinyPerson(
        _config(), TestModel(custom_output_text="Hi, I run ML platforms.")
    )
    adapter = TinyPersonEmailAdapter(person, _config())

    msg = await adapter.anext_email("Tick 1. Write an email.", tick=1, subject="Tick 1")

    assert msg is not None
    assert msg.get_content().strip() == "Hi, I run ML platforms."
    assert adapter.messages_sent == 1


@pytest.mark.parametrize(
    "output",
    ["PASS", "PASS.", "PASSLKPASS", "PASSCousin, please introduce us."],
)
async def test_llm_persona_pass_sentinel_skips_send_and_preserves_budget(output: str):
    person = LLMTinyPerson(_config(), TestModel(custom_output_text=output))
    adapter = TinyPersonEmailAdapter(person, _config())

    msg = await adapter.anext_email("Tick 2. Anything new?", tick=2)

    assert msg is None
    assert adapter.messages_sent == 0


async def test_llm_persona_delivers_email_that_mentions_pass_mid_sentence():
    body = "Could you pass this along to the team?"
    person = LLMTinyPerson(_config(), TestModel(custom_output_text=body))
    adapter = TinyPersonEmailAdapter(person, _config())

    msg = await adapter.anext_email("Tick 2. Anything new?", tick=2)

    assert msg is not None
    assert msg.get_content().strip() == body
    assert adapter.messages_sent == 1


async def test_llm_persona_keeps_history_across_ticks():
    person = LLMTinyPerson(_config(), TestModel(custom_output_text="Same answer."))

    await person.alisten_and_act("Tick 1.")
    first_len = len(person._history)
    await person.alisten_and_act("Tick 2.")

    assert len(person._history) > first_len


async def test_llm_persona_retries_transient_model_http_error(monkeypatch):
    calls = 0

    async def no_sleep(_delay: float) -> None:
        pass

    async def flaky_model(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ModelHTTPError(
                status_code=429,
                model_name="test-persona",
                body="provider unavailable",
            )
        return ModelResponse(parts=[TextPart(content="Recovered response")])

    monkeypatch.setattr(LLMTinyPerson._run_agent.retry, "sleep", no_sleep)
    person = LLMTinyPerson(_config(), FunctionModel(flaky_model))

    result = await person.alisten_and_act("Tick 1.")

    assert result == {"content": "Recovered response"}
    assert calls == 3


async def test_llm_persona_wraps_exhausted_transient_error(monkeypatch):
    calls = 0

    async def no_sleep(_delay: float) -> None:
        pass

    async def unavailable_model(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise ModelHTTPError(
            status_code=429,
            model_name="test-persona",
            body="provider unavailable",
        )

    monkeypatch.setattr(LLMTinyPerson._run_agent.retry, "sleep", no_sleep)
    person = LLMTinyPerson(_config(), FunctionModel(unavailable_model))

    with pytest.raises(TransientPersonaError) as exc_info:
        await person.alisten_and_act("Tick 1.")

    assert exc_info.value.error_type == "ModelHTTPError"
    assert calls == 3


async def test_anext_email_falls_back_to_sync_listener():
    class SyncPerson:
        name = "Priya"

        def listen_and_act(self, stimulus: str):
            return {"content": "sync body"}

    adapter = TinyPersonEmailAdapter(SyncPerson(), _config())

    msg = await adapter.anext_email("Tick 1.", tick=1)

    assert msg is not None
    assert msg.get_content().strip() == "sync body"
