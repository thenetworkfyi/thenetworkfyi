"""pydantic-ai agent wiring: model selection, tool registration, run entrypoint."""
from __future__ import annotations

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.prompts import SYSTEM_PROMPT
from thenetwork.agent.tools import (
    dispatch_email,
    escalate,
    forget,
    register_person,
    remember,
    search,
)
from thenetwork.audit import audit_event, audit_model_trace, audit_run, audit_span
from thenetwork.email.outbound import notify_admins
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
    agent.tool(register_person)

    return agent


async def run_agent_for_email(
    sender_email: str,
    sender_user_id: str | None,
    email_subject: str,
    email_body: str,
    sender_authenticated: bool = False,
) -> str:
    """Run the agent for one inbound email.

    The untrusted email body is passed as user-role message content - it is
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
            sender_authenticated=sender_authenticated,
            inbound_subject=email_subject,
        )
        settings = get_settings()
        agent = build_agent(model=settings.agent_model)
        usage_limits = UsageLimits(
            request_limit=settings.agent_request_limit,
            total_tokens_limit=settings.agent_total_tokens_limit,
        )
        user_message = f"Subject: {email_subject}\n\n{email_body}"
        audit_event(
            "agent.prompt_constructed",
            sender_known=sender_user_id is not None,
            subject_chars=len(email_subject),
            body_chars=len(email_body),
            user_message_chars=len(user_message),
        )
        try:
            result = await agent.run(user_message, deps=deps, usage_limits=usage_limits)
        except UsageLimitExceeded as exc:
            audit_event(
                "agent.usage_limit_exceeded",
                outcome="error",
                error_type=type(exc).__name__,
            )
            sender_known = sender_user_id is not None
            subject = "[The Network] Agent run interrupted: usage limit exceeded"
            body = (
                f"Email from {sender_email} hit the configured usage limit "
                "mid-run and was interrupted before producing a reply.\n\n"
                f"Sender known: {sender_known}\n\n"
                f"Reason: {exc}\n\n"
                "The run may have partially completed actions (for example, "
                "half of a two-person introduction) before being cut off. "
                "Please review and follow up manually."
            )
            notify_admins(settings, subject, body)
            return ""
        audit_model_trace(result)
        tool_called = any(
            getattr(part, "part_kind", None) == "tool-call"
            for message in result.all_messages()
            for part in getattr(message, "parts", ())
        )
        audit_event(
            "agent.response_generated",
            body_chars=len(result.output),
            tool_called=tool_called,
        )
        if not tool_called:
            audit_event(
                "agent.no_action_taken",
                sender_known=sender_user_id is not None,
                subject_chars=len(email_subject),
                body_chars=len(email_body),
            )
        return result.output
