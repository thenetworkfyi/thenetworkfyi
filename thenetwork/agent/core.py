"""pydantic-ai agent wiring: model selection, tool registration, run entrypoint."""
from __future__ import annotations

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.prompts import SYSTEM_PROMPT
from thenetwork.agent.tools import (
    escalate,
    forget,
    propose_introduction,
    register_person,
    remember,
    reply_to_sender,
    search,
    send_outreach,
)
from thenetwork.audit import (
    audit_event,
    audit_model_trace,
    audit_run,
    audit_sender,
    audit_span,
    audit_trace,
)
from thenetwork.email.outbound import notify_admins
from thenetwork.model_config import model_with_api_key
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.settings import get_settings

_UNDISPATCHED_RESPONSE_SUBJECT = "[The Network] Agent response needs review"
_UNDISPATCHED_RESPONSE_BODY = (
    "An agent run generated final text without a reply, outreach, or escalate action. "
    "The text was not sent. Review the correlated audit trace."
)


def build_agent(model: Any = None) -> Agent[AgentDeps, str]:
    """Construct the pydantic-ai agent with all tools registered."""
    settings = None
    if model is None:
        settings = get_settings()
        model = settings.agent_model
    if isinstance(model, str):
        settings = settings or get_settings()
        model = model_with_api_key(model, settings.agent_api_key)

    agent: Agent[AgentDeps, str] = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=AgentDeps,
        output_type=str,
    )

    # One retry is exclusively for malformed tool arguments. World-state and
    # policy outcomes are structured status results, never ModelRetry signals.
    agent.tool(remember, retries=1)
    agent.tool(forget, retries=1)
    agent.tool(search, retries=1)
    agent.tool(propose_introduction, retries=1)
    agent.tool(escalate, retries=1)
    agent.tool(reply_to_sender, retries=1)
    agent.tool(send_outreach, retries=1)
    agent.tool(register_person, retries=1)

    return agent


async def run_agent_for_email(
    sender_email: str,
    sender_user_id: str | None,
    email_subject: str,
    email_body: str,
    sender_authenticated: bool = False,
    sender_display_name: str | None = None,
    inbound_message_id: str | None = None,
    inbound_references: str | None = None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    trace_id: str | None = None,
    is_proactive: bool = False,
    proactive_candidate_id: str | None = None,
) -> str:
    """Run the agent for one inbound email.

    The untrusted email body is passed as user-role message content - it is
    NEVER concatenated into the system prompt (role separation, THE SEAL).
    """
    with audit_run(), audit_trace(trace_id), audit_sender(
        optional_sender_identifier(sender_email)
    ), audit_span(
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
            inbound_message_id=inbound_message_id,
            inbound_references=inbound_references,
            inbound_body_for_quote=inbound_body_for_quote,
            inbound_date=inbound_date,
            trace_id=trace_id,
            is_proactive=is_proactive,
            proactive_candidate_id=proactive_candidate_id,
        )
        settings = get_settings()
        agent = build_agent(model=settings.agent_model)
        usage_limits = UsageLimits(
            request_limit=settings.agent_request_limit,
            total_tokens_limit=settings.agent_total_tokens_limit,
        )
        sender_name_line = (
            f"From display name: {sender_display_name}\n" if sender_display_name else ""
        )
        user_message = f"{sender_name_line}Subject: {email_subject}\n\n{email_body}"
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
            notify_admins(settings, subject, body, trace_id=trace_id)
            return ""
        audit_model_trace(result)
        tool_names = {
            tool_name
            for message in result.all_messages()
            for part in getattr(message, "parts", ())
            if getattr(part, "part_kind", None) == "tool-call"
            if (tool_name := getattr(part, "tool_name", None))
        }
        tool_called = bool(tool_names)
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
        has_undispatched_text = (
            result.output.strip()
            and not {"reply_to_sender", "send_outreach", "escalate"}.intersection(tool_names)
            and deps.server_side_send_count == 0
        )
        if has_undispatched_text and deps.is_proactive:
            audit_event(
                "agent.proactive_no_action",
                sender_known=sender_user_id is not None,
                body_chars=len(result.output),
            )
        elif has_undispatched_text:
            audit_event(
                "agent.undispatched_response",
                body_chars=len(result.output),
                sender_known=sender_user_id is not None,
            )
            notify_admins(
                settings,
                _UNDISPATCHED_RESPONSE_SUBJECT,
                _UNDISPATCHED_RESPONSE_BODY,
                trace_id=trace_id,
            )
        return result.output
