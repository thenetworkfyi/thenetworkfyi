from pydantic_ai.models.test import TestModel

from thenetwork.sim.llm_persona import LLMTinyPerson
from thenetwork.sim.persona import PersonaConfig, TinyPersonEmailAdapter


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


async def test_llm_persona_writes_email_body_from_model_output():
    person = LLMTinyPerson(_config(), TestModel(custom_output_text="Hi, I run ML platforms."))
    adapter = TinyPersonEmailAdapter(person, _config())

    msg = await adapter.anext_email("Tick 1. Write an email.", tick=1, subject="Tick 1")

    assert msg is not None
    assert msg.get_content().strip() == "Hi, I run ML platforms."
    assert adapter.messages_sent == 1


async def test_llm_persona_pass_sentinel_skips_send_and_preserves_budget():
    person = LLMTinyPerson(_config(), TestModel(custom_output_text="PASS"))
    adapter = TinyPersonEmailAdapter(person, _config())

    msg = await adapter.anext_email("Tick 2. Anything new?", tick=2)

    assert msg is None
    assert adapter.messages_sent == 0


async def test_llm_persona_keeps_history_across_ticks():
    person = LLMTinyPerson(_config(), TestModel(custom_output_text="Same answer."))

    await person.alisten_and_act("Tick 1.")
    first_len = len(person._history)
    await person.alisten_and_act("Tick 2.")

    assert len(person._history) > first_len


async def test_anext_email_falls_back_to_sync_listener():
    class SyncPerson:
        name = "Priya"

        def listen_and_act(self, stimulus: str):
            return {"content": "sync body"}

    adapter = TinyPersonEmailAdapter(SyncPerson(), _config())

    msg = await adapter.anext_email("Tick 1.", tick=1)

    assert msg is not None
    assert msg.get_content().strip() == "sync body"
