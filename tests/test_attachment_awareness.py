"""Assembled inbound-to-reply coverage for the count-only attachment signal."""

from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from imap_tools.message import MailAttachment
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from thenetwork.agent.core import build_agent
from thenetwork.agent.deps import AgentCapabilities
from thenetwork.email.inbound import InboundMessage, count_stripped_attachments
from thenetwork.introductions import ConsentReplyResult
from thenetwork.worker.producer import _poll_mailbox_and_enqueue
from thenetwork.worker.tasks import process_email


@pytest.mark.asyncio
async def test_real_attachment_count_reaches_agent_without_attachment_metadata(caplog):
    mime = EmailMessage()
    mime.set_content("Please review the attached material.")
    mime.add_attachment(
        b"private attachment bytes",
        maintype="application",
        subtype="pdf",
        filename="private-roadmap.pdf",
    )
    [part] = list(mime.iter_attachments())
    attachment_count = count_stripped_attachments(
        SimpleNamespace(attachments=[MailAttachment(part)])
    )
    assert attachment_count == 1

    inbound = InboundMessage(
        uid="attachment-1",
        sender="sender@example.com",
        subject="Project details",
        body="Please review the attached material.",
        auto_submitted=None,
        sender_authenticated=True,
        attachment_count=attachment_count,
    )
    producer_settings = SimpleNamespace(
        primary_intake_burst_monitoring_enabled=False,
        sender_identifier_secret="",
        relay_domain="relay.example.com",
        daily_agent_token_cap=0,
    )
    with (
        patch("thenetwork.worker.producer.poll_unseen", return_value=[inbound]),
        patch(
            "thenetwork.worker.producer.get_settings", return_value=producer_settings
        ),
        patch("thenetwork.worker.producer.process_email") as deferred,
        patch("thenetwork.worker.producer.mark_messages_seen"),
        patch("thenetwork.worker.producer.check_daily_token_budget", return_value=True),
    ):
        assert _poll_mailbox_and_enqueue("primary") == 1
    job_kwargs = deferred.defer.call_args.kwargs
    assert job_kwargs["attachment_count"] == 1

    captured_prompt = ""
    model_calls = 0

    async def attachment_model(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        nonlocal captured_prompt, model_calls
        model_calls += 1
        if model_calls > 1:
            return ModelResponse(parts=[TextPart(content="Reply sent.")])
        captured_prompt = "\n".join(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="reply_to_sender",
                    args={
                        "subject": "Re: Project details",
                        "body_text": (
                            "The attachment was not read. Please paste the relevant "
                            "content into the email."
                        ),
                        "sent_email_summary": "explained that an attachment was not read",
                    },
                )
            ]
        )

    agent = build_agent(model=FunctionModel(attachment_model))
    worker_settings = SimpleNamespace(
        relay_domain="relay.example.com",
        daily_agent_token_cap=0,
        agent_model="test:model",
        agent_request_limit=4,
        agent_total_tokens_limit=20_000,
        response_log_redaction_secret="",
    )
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.get.return_value = None
    sent: list[dict[str, str]] = []

    def capture_reply(*, to_address: str, subject: str, body_text: str, **_kwargs):
        sent.append({"to": to_address, "subject": subject, "body": body_text})

    capabilities = AgentCapabilities(
        send_reply=MagicMock(side_effect=capture_reply),
        record_sent_email_memory=AsyncMock(),
    )
    with (
        patch("thenetwork.worker.tasks.get_settings", return_value=worker_settings),
        patch("thenetwork.worker.tasks.get_session", return_value=session),
        patch("thenetwork.worker.tasks.is_primary_intake_paused", return_value=False),
        patch("thenetwork.worker.tasks.check_daily_token_budget", return_value=True),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        patch("thenetwork.worker.tasks.verify_admin_request", return_value=None),
        patch(
            "thenetwork.worker.tasks.process_consent_reply",
            return_value=ConsentReplyResult(handled=False),
        ),
        patch("thenetwork.worker.tasks.record_sent_email_memories", AsyncMock()),
        patch(
            "thenetwork.worker.tasks._sender_id_for_authenticated_sender",
            return_value=None,
        ),
        patch("thenetwork.agent.core.get_settings", return_value=worker_settings),
        patch("thenetwork.agent.core.build_agent", return_value=agent),
        patch("thenetwork.agent.tools._check_daily_dispatch_cap", return_value=True),
        patch("thenetwork.agent.tools._consume_daily_dispatch_cap"),
    ):
        await process_email.func(**job_kwargs, capabilities=capabilities)

    assert len(sent) == 1
    assert "attachment was not read" in sent[0]["body"].lower()
    assert "Attachments present but not read: 1" in captured_prompt
    for attacker_authored in ("private-roadmap.pdf", "application/pdf"):
        assert attacker_authored not in captured_prompt
        assert attacker_authored not in caplog.text
