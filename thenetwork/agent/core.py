"""pydantic-ai agent wiring: model selection, tool registration, run entrypoint."""
from __future__ import annotations

from typing import Any

from pydantic_ai import Agent

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.prompts import SYSTEM_PROMPT
from thenetwork.agent.tools import dispatch_email, escalate, forget, remember, search
from thenetwork.audit import audit_event, audit_model_trace, audit_run, audit_span
from thenetwork.settings import get_settings


def build_agent(model: Any = None) -> Agent[AgentDeps, str]:
    """Construct the pydantic-ai agent with all tools registered."""
    if model is None:
        settings = get_settings()
        model = settings.agent_model

    agent: Agent[AgentDeps, str] = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=AgentDeps,
        output_type=str,
    )

    agent.tool(remember)
    agent.tool(forget)
    agent.tool(search)
    agent.tool(escalate)
    agent.tool(dispatch_email)

    return agent


async def run_agent_for_email(
    sender_email: str,
    sender_user_id: str | None,
    email_subject: str,
    email_body: str,
) -> str:
    """Run the agent for one inbound email.

    The untrusted email body is passed as user-role message content — it is
    NEVER concatenated into the system prompt (role separation, THE SEAL).
    """
    with audit_run(), audit_span(
        "agent.run",
        sender_known=sender_user_id is not None,
        subject_chars=len(email_subject),
        body_chars=len(email_body),
    ):
        deps = AgentDeps(
            sender_email=sender_email,
            sender_user_id=sender_user_id,
        )
        agent = build_agent()
        user_message = f"Subject: {email_subject}\n\n{email_body}"
        audit_event(
            "agent.prompt_constructed",
            sender_known=sender_user_id is not None,
            subject_chars=len(email_subject),
            body_chars=len(email_body),
            user_message_chars=len(user_message),
        )
        result = await agent.run(user_message, deps=deps)
        audit_model_trace(result)
        audit_event("agent.response_generated", body_chars=len(result.output))
        return result.output
