"""pydantic-ai agent wiring: model selection, tool registration, run entrypoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from thenetwork.agent.deps import AgentDeps
from thenetwork.agent.prompts import SYSTEM_PROMPT
from thenetwork.agent.tools import (
    cancel_event,
    create_event,
    escalate,
    forget,
    no_action,
    propose_introduction,
    register_person,
    remember,
    reply_to_sender,
    resume_event_recommendations,
    search,
    search_events,
    send_event_recommendation,
    send_first_contact_welcome,
    send_outreach,
    stop_event_recommendations,
    update_event,
)
from thenetwork.audit import (
    audit_event,
    audit_model_trace,
    audit_run,
    audit_sender,
    audit_span,
    audit_trace,
)
from thenetwork.db.session import get_session
from thenetwork.email.inbound import MAX_ATTACHMENT_COUNT
from thenetwork.llm_observability import (
    LLMWorkload,
    observe_agent_duration,
    observe_model,
)
from thenetwork.model_config import model_with_api_key
from thenetwork.memory.recent_context import (
    RECENT_MEMORY_CONTEXT_MAX_CHARS,
    RECENT_MEMORY_CONTEXT_MAX_COUNT,
    load_recent_sender_memory_context,
)
from thenetwork.security.sender_identifier import optional_sender_identifier
from thenetwork.settings import get_settings
from thenetwork.worker.metrics import record_agent_usage_limit_exceeded


def build_agent(
    model: Any = None,
    *,
    is_proactive: bool = False,
    proactive_candidate_id: str | None = None,
    proactive_event_id: str | None = None,
) -> Agent[AgentDeps, str]:
    """Construct an agent with the capabilities appropriate to this run."""
    settings = None
    if model is None:
        settings = get_settings()
        model = settings.agent_model
    if isinstance(model, str):
        settings = settings or get_settings()
        model = model_with_api_key(
            model,
            settings.agent_api_key,
            settings.model_request_timeout_seconds,
            workload=LLMWorkload.EMAIL_AGENT,
        )
    else:
        model = observe_model(model, workload=LLMWorkload.EMAIL_AGENT)

    thinking_level = settings.agent_thinking_level if settings is not None else None

    agent: Agent[AgentDeps, str] = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=AgentDeps,
        output_type=[
            str,
            ToolOutput(no_action, name="no_action", max_retries=1),
        ],
        model_settings=(
            ModelSettings(thinking=thinking_level)
            if thinking_level is not None
            else None
        ),
        retries=1,
    )

    @agent.output_validator
    def require_terminal_action(ctx: RunContext[AgentDeps], output: str) -> str:
        if (
            output.strip()
            and ctx.deps.server_side_send_count == 0
            and not ctx.deps.terminal_action_taken
        ):
            if ctx.deps.is_proactive:
                instruction = "Call the available bound action or no_action explicitly."
            else:
                instruction = (
                    "Call reply_to_sender, send_outreach, escalate, or no_action "
                    "explicitly."
                )
            raise ModelRetry(f"Bare final text is not a terminal action. {instruction}")
        return output

    # Synthetic proactive jobs are capability grants for exactly one
    # server-bound action, not ordinary inbound sessions. Withholding every
    # unrelated tool makes mutation structurally unavailable to a hijacked
    # model; the tool implementations retain their own binding checks.
    if is_proactive:
        if proactive_candidate_id is not None:
            agent.tool(propose_introduction, retries=1)
        elif proactive_event_id is not None:
            agent.tool(send_event_recommendation, retries=1)
    else:
        # One retry is exclusively for malformed tool arguments. World-state
        # and policy outcomes are structured status results, never ModelRetry.
        agent.tool(remember, retries=1)
        agent.tool(forget, retries=1)
        agent.tool(search, retries=1)
        agent.tool(propose_introduction, retries=1)
        agent.tool(escalate, retries=1)
        agent.tool(send_first_contact_welcome, retries=1)
        agent.tool(reply_to_sender, retries=1)
        agent.tool(send_outreach, retries=1)
        agent.tool(register_person, retries=1)
        agent.tool(create_event, retries=1)
        agent.tool(update_event, retries=1)
        agent.tool(cancel_event, retries=1)
        agent.tool(search_events, retries=1)
        agent.tool(stop_event_recommendations, retries=1)
        agent.tool(resume_event_recommendations, retries=1)

    return agent


async def run_agent_for_email(
    sender_email: str,
    sender_user_id: str | None,
    email_subject: str,
    email_body: str,
    sender_authenticated: bool = False,
    sender_display_name: str | None = None,
    attachment_count: int = 0,
    inbound_message_id: str | None = None,
    inbound_references: str | None = None,
    inbound_body_for_quote: str | None = None,
    inbound_date: str | None = None,
    trace_id: str | None = None,
    is_proactive: bool = False,
    proactive_candidate_id: str | None = None,
    proactive_event_id: str | None = None,
    proactive_event_version: int | None = None,
    session_factory: Callable | None = None,
) -> str:
    """Run the agent for one inbound email.

    The untrusted email body and bounded sender-memory gist projection are
    passed as user-role message content. Neither is ever concatenated into the
    system prompt (role separation, THE SEAL).
    """
    with (
        audit_run(),
        audit_trace(trace_id),
        audit_sender(optional_sender_identifier(sender_email)),
        observe_agent_duration(),
        audit_span(
            "agent.run",
            sender_known=sender_user_id is not None,
            subject_chars=len(email_subject),
            body_chars=len(email_body),
        ),
    ):
        attachment_count = max(0, min(attachment_count, MAX_ATTACHMENT_COUNT))
        deps = AgentDeps(
            sender_email=sender_email,
            sender_user_id=sender_user_id,
            sender_authenticated=sender_authenticated,
            inbound_subject=email_subject,
            inbound_body=email_body,
            inbound_message_id=inbound_message_id,
            inbound_references=inbound_references,
            inbound_body_for_quote=inbound_body_for_quote,
            inbound_date=inbound_date,
            trace_id=trace_id,
            is_proactive=is_proactive,
            proactive_candidate_id=proactive_candidate_id,
            proactive_event_id=proactive_event_id,
            proactive_event_version=proactive_event_version,
            session_factory=session_factory,
        )
        settings = get_settings()
        if is_proactive:
            agent = build_agent(
                model=settings.agent_model,
                is_proactive=True,
                proactive_candidate_id=proactive_candidate_id,
                proactive_event_id=proactive_event_id,
            )
        else:
            agent = build_agent(model=settings.agent_model)
        usage_limits = UsageLimits(
            request_limit=settings.agent_request_limit,
            total_tokens_limit=settings.agent_total_tokens_limit,
        )
        sender_name_line = (
            f"From display name: {sender_display_name}\n" if sender_display_name else ""
        )
        attachment_line = (
            f"Attachments present but not read: {attachment_count}\n"
            if attachment_count
            else ""
        )
        memory_context = load_recent_sender_memory_context(
            sender_user_id,
            session_factory=session_factory or get_session,
            max_count=getattr(
                settings,
                "recent_memory_context_max_count",
                RECENT_MEMORY_CONTEXT_MAX_COUNT,
            ),
            max_chars=getattr(
                settings,
                "recent_memory_context_max_chars",
                RECENT_MEMORY_CONTEXT_MAX_CHARS,
            ),
        )
        inbound_message = f"{sender_name_line}{attachment_line}Subject: {email_subject}\n\n{email_body}"
        user_message = (
            f"{memory_context.text}\n\n{inbound_message}"
            if memory_context.text
            else inbound_message
        )
        audit_event(
            "agent.prompt_constructed",
            sender_known=sender_user_id is not None,
            subject_chars=len(email_subject),
            body_chars=len(email_body),
            attachment_count=attachment_count,
            recent_memory_gist_count=memory_context.gist_count,
            recent_memory_context_chars=len(memory_context.text),
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
            record_agent_usage_limit_exceeded()
            return ""
        except Exception as exc:
            if deps.server_side_send_count == 0:
                raise

            # SMTP is an external side effect and cannot be rolled back. Once a
            # run has sent anything, retrying the whole Procrastinate job with
            # fresh AgentDeps would forget the in-run replay cache and could
            # deliver the same message again. Treat a later model/provider
            # failure as an interrupted completion step instead: the successful
            # send and its durable summary are already the authoritative result.
            audit_event(
                "agent.failed_after_send",
                outcome="error",
                error_type=type(exc).__name__,
            )
            return ""
        audit_model_trace(
            result,
            pseudonym_secret=getattr(settings, "response_log_redaction_secret", None),
        )
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
            and not {
                "reply_to_sender",
                "send_outreach",
                "escalate",
                "no_action",
            }.intersection(tool_names)
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
        return result.output
