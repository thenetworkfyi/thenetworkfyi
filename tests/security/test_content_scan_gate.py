from __future__ import annotations

from enum import Enum
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from thenetwork.audit import LOGGER_NAME


class _Decision(Enum):
    ALLOW = "allow"
    BLOCK = "block"


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Tokenizer:
    def encode(self, text, *, add_special_tokens):
        tokens = text.split() if text else []
        return ["<s>", *tokens, "</s>"] if add_special_tokens else tokens

    def decode(
        self,
        token_ids,
        *,
        skip_special_tokens,
        clean_up_tokenization_spaces,
    ):
        return " ".join(token_ids)

    def num_special_tokens_to_add(self, *, pair):
        return 2


class _PromptGuard:
    def __init__(self):
        self.tokenizer = _Tokenizer()

    def _preprocess_text_for_promptguard(self, text):
        return text


class _LateBlockingScanner:
    def __init__(self):
        self.pg = _PromptGuard()
        self.windows = []

    async def scan(self, message):
        self.windows.append(message.content)
        decision = (
            _Decision.BLOCK if "LATE-INJECTION" in message.content else _Decision.ALLOW
        )
        return SimpleNamespace(
            decision=decision,
            reason=f'Full text: "{message.content}"',
        )


def _empty_session():
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.get.return_value = None
    return session


def _audit_events(caplog):
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


@pytest.mark.asyncio
async def test_late_window_injection_is_audited_and_blocked_before_agent_paths(caplog):
    from thenetwork.worker.tasks import process_email

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    scanner = _LateBlockingScanner()
    body = " ".join([*(f"benign-{index}" for index in range(600)), "LATE-INJECTION"])
    consent = MagicMock()
    memories = AsyncMock()
    agent = AsyncMock()

    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._get_scanner",
            return_value=scanner,
        ),
        patch(
            "thenetwork.security.content_scan._get_llamafirewall_types",
            return_value=(_Decision, _Message),
        ),
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch("thenetwork.worker.tasks.process_consent_reply", consent),
        patch("thenetwork.worker.tasks.record_sent_email_memories", memories),
        patch("thenetwork.worker.tasks.run_agent_for_email", agent),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
    ):
        await process_email.func(
            sender_email="unknown@example.com",
            sender_authenticated=False,
            subject="Private subject",
            body=body,
        )

    assert len(scanner.windows) >= 2
    assert "LATE-INJECTION" not in scanner.windows[0]
    assert "LATE-INJECTION" in scanner.windows[-1]
    consent.assert_not_called()
    memories.assert_not_awaited()
    agent.assert_not_awaited()
    send_reply.assert_not_called()

    rejection_events = [
        event
        for event in _audit_events(caplog)
        if event["event"] == "worker.message_rejected"
    ]
    assert [event["reason"] for event in rejection_events] == [
        "prompt_injection_detected"
    ]
    serialized = "\n".join(record.message for record in caplog.records)
    assert "LATE-INJECTION" not in serialized
    assert "Private subject" not in serialized


@pytest.mark.parametrize("reason", ["prompt_injection_detected", "scanner_error"])
@pytest.mark.asyncio
async def test_block_and_scanner_failure_stop_consent_memory_and_agent(reason):
    from thenetwork.worker.tasks import process_email

    consent = MagicMock()
    memories = AsyncMock()
    agent = AsyncMock()
    with (
        patch("thenetwork.worker.tasks.get_session", return_value=_empty_session()),
        patch("thenetwork.worker.tasks.check_rate_limit", return_value=True),
        patch(
            "thenetwork.worker.tasks.scan_content",
            new=AsyncMock(return_value=(False, reason)),
        ),
        patch("thenetwork.worker.tasks.process_consent_reply", consent),
        patch("thenetwork.worker.tasks.record_sent_email_memories", memories),
        patch("thenetwork.worker.tasks.run_agent_for_email", agent),
        patch("thenetwork.worker.tasks.send_reply") as send_reply,
    ):
        await process_email.func(
            sender_email="unknown@example.com",
            sender_authenticated=False,
            subject="Hello",
            body="ordinary body",
        )

    consent.assert_not_called()
    memories.assert_not_awaited()
    agent.assert_not_awaited()
    send_reply.assert_not_called()
