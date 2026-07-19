"""TinyPerson email adapter for the simulation harness."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from enum import StrEnum
from html import escape
from typing import Any, Protocol
from urllib.parse import urlsplit

from thenetwork.email.threading import clean_message_id, clean_references


class TinyPersonLike(Protocol):
    name: str

    def listen_and_act(self, stimulus: str, *args: Any, **kwargs: Any) -> Any:
        """Receive a stimulus and return the persona's next action."""


class EmailFormat(StrEnum):
    """MIME formats supported by simulated persona mail."""

    PLAIN = "plain"
    MULTIPART_ALTERNATIVE = "multipart/alternative"


@dataclass(frozen=True)
class SignatureLink:
    """Server-authored link rendered as signature text and an HTML anchor."""

    text: str
    url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("signature link must use an absolute http(s) URL")


@dataclass(frozen=True)
class EmailSignature:
    """Structured signature content controlled by the simulation fixture."""

    lines: tuple[str, ...]
    link: SignatureLink | None = None


@dataclass(frozen=True)
class EmailPresentation:
    """Server-authored MIME and signature choices for a persona."""

    format: EmailFormat = EmailFormat.PLAIN
    signature: EmailSignature | None = None


@dataclass(frozen=True)
class PersonaConfig:
    """Harness-specific persona configuration."""

    name: str
    email: str
    goal: str
    stop_condition: str
    message_budget: int = 4
    agent_address: str = "join@thenetwork.test"
    presentation: EmailPresentation = field(default_factory=EmailPresentation)


class TinyPersonEmailAdapter:
    """Thin adapter from TinyPerson actions to outbound EmailMessage objects."""

    def __init__(self, person: TinyPersonLike, config: PersonaConfig) -> None:
        self.person = person
        self.config = config
        self.messages_sent = 0

    @property
    def exhausted(self) -> bool:
        return self.messages_sent >= self.config.message_budget

    def next_email(
        self,
        stimulus: str,
        *,
        tick: int,
        subject: str = "The Network",
        reply_to: EmailMessage | None = None,
        fallback_body: Callable[[PersonaConfig], str] | None = None,
        body_filter: Callable[[str], str] | None = None,
    ) -> EmailMessage | None:
        if self.exhausted:
            return None

        action = self.person.listen_and_act(stimulus)
        body = extract_action_text(action)
        if not body and fallback_body is not None:
            body = fallback_body(self.config)
        if not body:
            body = (
                f"I am {self.config.name}. {self.config.goal} "
                f"My stop condition is: {self.config.stop_condition}"
            )
        if body_filter is not None:
            body = body_filter(body)
        if not body:
            return None
        return self._build_message(body, tick=tick, subject=subject, reply_to=reply_to)

    async def anext_email(
        self,
        stimulus: str,
        *,
        tick: int,
        subject: str = "The Network",
        reply_to: EmailMessage | None = None,
        fallback_body: Callable[[PersonaConfig], str] | None = None,
        body_filter: Callable[[str], str] | None = None,
    ) -> EmailMessage | None:
        """Async variant; personas exposing `alisten_and_act` may decline to write."""
        listener = getattr(self.person, "alisten_and_act", None)
        if listener is None:
            return self.next_email(
                stimulus,
                tick=tick,
                subject=subject,
                reply_to=reply_to,
                fallback_body=fallback_body,
                body_filter=body_filter,
            )
        if self.exhausted:
            return None
        body = extract_action_text(await listener(stimulus))
        if body and body_filter is not None:
            body = body_filter(body)
        if not body:
            return None
        return self._build_message(body, tick=tick, subject=subject, reply_to=reply_to)

    def _build_message(
        self,
        body: str,
        *,
        tick: int,
        subject: str,
        reply_to: EmailMessage | None = None,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = f"{self.config.name} <{self.config.email}>"
        reply_address = (
            parseaddr(reply_to.get("Reply-To", ""))[1] if reply_to is not None else ""
        )
        msg["To"] = reply_address or self.config.agent_address
        msg["Subject"] = _reply_subject(reply_to, fallback=subject)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg["X-Sim-Tick"] = str(tick)
        msg["X-Sim-Direction"] = "persona->agent"
        msg["X-Sim-Persona"] = self.config.name
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
            _plain_message(
                authored_body, quoted_body, self.config.presentation.signature
            )
        )
        if self.config.presentation.format == EmailFormat.MULTIPART_ALTERNATIVE:
            msg.add_alternative(
                _html_message(
                    authored_body, quoted_body, self.config.presentation.signature
                ),
                subtype="html",
            )
        self.messages_sent += 1
        return msg


def _reply_subject(reply_to: EmailMessage | None, *, fallback: str) -> str:
    if reply_to is None:
        return fallback
    original_subject = str(reply_to.get("Subject", "")).strip()
    return f"Re: {original_subject}" if original_subject else fallback


def _message_parts(body: str, reply_to: EmailMessage | None) -> tuple[str, str | None]:
    authored_body = body.strip()
    if reply_to is None:
        return authored_body, None
    original_body = _plain_text_body(reply_to).strip()
    if not original_body:
        return authored_body, None
    return authored_body, original_body


def _plain_message(
    authored_body: str,
    quoted_body: str | None,
    signature: EmailSignature | None,
) -> str:
    sections = [authored_body]
    if signature is not None:
        sections.append(_plain_signature(signature))
    if quoted_body is not None:
        sections.append(_plain_quote(quoted_body))
    return "\n\n".join(section for section in sections if section)


def _plain_signature(signature: EmailSignature) -> str:
    lines = [*signature.lines]
    if signature.link is not None:
        lines.append(signature.link.text)
    return "-- \n" + "\n".join(lines)


def _plain_quote(body: str) -> str:
    quote = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    return quote


def _html_message(
    authored_body: str,
    quoted_body: str | None,
    signature: EmailSignature | None,
) -> str:
    sections = [_html_text(authored_body)]
    if signature is not None:
        sections.append(_html_signature(signature))
    if quoted_body is not None:
        sections.append(f"<blockquote>{_html_text(quoted_body)}</blockquote>")
    return "<!doctype html><html><body>" + "".join(sections) + "</body></html>"


def _html_text(text: str) -> str:
    paragraphs = []
    for paragraph in text.split("\n\n"):
        lines = "<br>".join(escape(line) for line in paragraph.splitlines())
        if lines:
            paragraphs.append(f"<p>{lines}</p>")
    return "".join(paragraphs)


def _html_signature(signature: EmailSignature) -> str:
    lines = [escape(line) for line in signature.lines]
    if signature.link is not None:
        lines.append(
            f'<a href="{escape(signature.link.url, quote=True)}">'
            f"{escape(signature.link.text)}</a>"
        )
    content = "<br>".join(lines)
    return f'<div class="signature"><p>--<br>{content}</p></div>'


def _plain_text_body(message: EmailMessage) -> str:
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content()
    if message.get_content_maintype() == "text":
        return message.get_content()
    return ""


def extract_action_text(action_result: Any) -> str:
    """Best-effort extraction of written text from TinyTroupe action results."""
    if isinstance(action_result, str):
        return action_result.strip()
    if isinstance(action_result, Mapping):
        return _extract_text_from_mapping(action_result)
    if isinstance(action_result, Iterable):
        for item in action_result:
            text = extract_action_text(item)
            if text:
                return text
    return ""


def _extract_text_from_mapping(action: Mapping[str, Any]) -> str:
    for key in ("content", "text", "message", "utterance"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested = action.get("action")
    if isinstance(nested, Mapping):
        return _extract_text_from_mapping(nested)

    return ""
