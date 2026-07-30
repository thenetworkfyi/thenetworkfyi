"""CrewAI Custom Tool for simulation mailbox access and MIME email construction."""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

from crewai.tools import BaseTool
from pydantic import Field

from thenetwork.email.threading import clean_message_id, clean_references
from thenetwork.sim.personas.consent import (
    make_reply_thread_faithful,
    thread_token_of,
)
from thenetwork.sim.personas.persona import (
    EmailFormat,
    PersonaConfig,
    _html_message,
    _message_parts,
    _plain_message,
    _reply_subject,
)
from thenetwork.sim.run.mail import _extract_body


def build_sim_email_message(
    config: PersonaConfig,
    body: str,
    *,
    tick: int,
    subject: str = "The Network",
    reply_to: EmailMessage | None = None,
) -> EmailMessage:
    """Construct an RFC 5322 EmailMessage with In-Reply-To, References, X-Sim headers, and signature options matching TinyPersonEmailAdapter output."""
    msg = EmailMessage()
    msg["From"] = f"{config.name} <{config.email}>"
    reply_address = (
        parseaddr(reply_to.get("Reply-To", ""))[1] if reply_to is not None else ""
    )
    msg["To"] = reply_address or config.agent_address
    msg["Subject"] = _reply_subject(reply_to, fallback=subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["X-Sim-Tick"] = str(tick)
    msg["X-Sim-Direction"] = "persona->agent"
    msg["X-Sim-Persona"] = config.name

    if reply_to is not None:
        message_id = clean_message_id(reply_to.get("Message-ID"))
        if message_id:
            msg["In-Reply-To"] = message_id
            references = clean_references(reply_to.get("References"))
            msg["References"] = (
                f"{references} {message_id}" if references else message_id
            )

    authored_body, quoted_body = _message_parts(body, reply_to)
    msg.set_content(
        _plain_message(authored_body, quoted_body, config.presentation.signature)
    )

    if config.presentation.format == EmailFormat.MULTIPART_ALTERNATIVE:
        msg.add_alternative(
            _html_message(authored_body, quoted_body, config.presentation.signature),
            subtype="html",
        )

    return msg


class SimMailboxTool(BaseTool):
    """Custom CrewAI tool allowing agents to read unread mailbox messages and send emails."""

    name: str = "SimMailboxTool"
    description: str = "Reads unread email messages from your simulation mailbox or sends an email reply/message to The Network."
    config: Any = Field(description="PersonaConfig for the persona")
    post_office: Any = Field(description="SimPostOffice instance")
    tick: int = Field(default=1, description="Current simulation tick")
    reply_to: Any = Field(default=None, description="Optional reply_to EmailMessage")
    messages_sent: int = Field(default=0, description="Counter of sent messages")
    allow_send: bool = Field(
        default=True,
        description="Whether this tool owns outbound transport for the current runtime",
    )

    def update_turn(self, *, tick: int, reply_to: EmailMessage | None = None) -> None:
        """Update the turn-specific message context while retaining shared state."""
        self.tick = tick
        self.reply_to = reply_to

    def _run(
        self, action: str = "read", body: str = "", subject: str = "The Network"
    ) -> str:
        if action == "read":
            messages = self.post_office.pop_all(self.config.email)
            if not messages:
                return "No unread messages."
            rendered = []
            for msg in messages:
                rendered.append(
                    f"From: {msg.get('From')}\nSubject: {msg.get('Subject')}\nBody:\n{_extract_body(msg)}"
                )
            return "\n---\n".join(rendered)
        elif action == "send":
            if not self.allow_send:
                return (
                    "Email transport is managed by the simulation runtime. "
                    "Return the plain-text email body as the task result instead."
                )
            self.messages_sent = self.post_office.sent_count(self.config.email)
            if self.messages_sent >= self.config.message_budget:
                return "Error: Message budget exhausted."
            active_thread = (
                thread_token_of(self.reply_to) if self.reply_to is not None else None
            )
            thread_kind = active_thread[0] if active_thread is not None else "intro"
            thread_token = active_thread[1] if active_thread is not None else None
            faithful_body = make_reply_thread_faithful(body, thread_token, thread_kind)
            email_msg = build_sim_email_message(
                self.config,
                faithful_body,
                tick=self.tick,
                subject=subject,
                reply_to=self.reply_to,
            )
            self.post_office.deliver(email_msg)
            self.messages_sent = self.post_office.record_sent(self.config.email)
            return "Email sent successfully."
        return f"Unknown action: {action}"
