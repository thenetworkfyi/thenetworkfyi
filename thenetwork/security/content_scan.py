"""Optional prompt-injection scanner (opt-in defense-in-depth).

Enabled by ``settings.content_scan_enabled``. When disabled, always passes and
does not import the optional LlamaFirewall dependency. This is not a primary
defense; the structural privacy boundary is documented in ``docs/security.md``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterator

from thenetwork.settings import get_settings

MODEL_CONTEXT_TOKENS = 512
WINDOW_OVERLAP_TOKENS = 64


class ContentScanReason(StrEnum):
    """PII-safe, stable reasons exposed to the worker and audit layer."""

    DISABLED = "disabled"
    OK = "ok"
    PROMPT_INJECTION_DETECTED = "prompt_injection_detected"
    SCANNER_ERROR = "scanner_error"


_scanner: Any | None = None


def _build_scanner() -> Any:
    """Import and construct Prompt Guard only after scanning is enabled."""
    from llamafirewall.scanners.prompt_guard_scanner import PromptGuardScanner

    return PromptGuardScanner()


def _get_scanner() -> Any:
    """Create the model-backed scanner once per worker process."""
    global _scanner
    if _scanner is None:
        _scanner = _build_scanner()
    return _scanner


def _get_llamafirewall_types() -> tuple[Any, Any]:
    """Load message and decision types without importing them when disabled."""
    from llamafirewall import ScanDecision, UserMessage

    return ScanDecision, UserMessage


def _prompt_guard_text(scanner: Any, text: str) -> str:
    """Apply the same preprocessing PromptGuardScanner uses before inference."""
    return scanner.pg._preprocess_text_for_promptguard(text)


def _encoded_length(scanner: Any, text: str) -> int:
    tokenizer = scanner.pg.tokenizer
    return len(
        tokenizer.encode(
            _prompt_guard_text(scanner, text),
            add_special_tokens=True,
        )
    )


def _iter_token_windows(text: str, scanner: Any) -> Iterator[str]:
    """Yield overlapping windows that fit Prompt Guard 2's full context.

    LlamaFirewall enables truncation internally. Windowing with the scanner's
    own tokenizer prevents that truncation from silently skipping later mail.
    Each decoded window is re-encoded after Prompt Guard preprocessing and
    shrunk if necessary, so special tokens are included in the 512-token cap.
    """
    tokenizer = scanner.pg.tokenizer
    preprocessed = _prompt_guard_text(scanner, text)
    token_ids = tokenizer.encode(preprocessed, add_special_tokens=False)
    special_tokens = tokenizer.num_special_tokens_to_add(pair=False)
    payload_tokens = MODEL_CONTEXT_TOKENS - special_tokens
    if payload_tokens <= 0:
        raise ValueError("Prompt Guard tokenizer leaves no content-token capacity")

    if not token_ids:
        yield ""
        return

    overlap = min(WINDOW_OVERLAP_TOKENS, payload_tokens - 1)
    start = 0
    while start < len(token_ids):
        window_ids = token_ids[start : start + payload_tokens]
        while window_ids:
            window = tokenizer.decode(
                window_ids,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            if _encoded_length(scanner, window) <= MODEL_CONTEXT_TOKENS:
                break
            window_ids = window_ids[:-1]
        else:
            raise ValueError("Prompt Guard tokenizer could not produce a safe window")

        yield window
        consumed = len(window_ids)
        if start + consumed >= len(token_ids):
            return
        start += max(1, consumed - overlap)


async def scan_content(text: str) -> tuple[bool, ContentScanReason]:
    """Return a safe decision and stable reason, failing closed when enabled.

    LlamaFirewall's ``ScanResult.reason`` contains the raw scanned text for a
    block. It must never be returned, logged, or otherwise leave this module.
    """
    if not get_settings().content_scan_enabled:
        return True, ContentScanReason.DISABLED

    try:
        scanner = _get_scanner()
        scan_decision, user_message = _get_llamafirewall_types()
        for window in _iter_token_windows(text, scanner):
            result = await scanner.scan(user_message(content=window))
            if result.decision == scan_decision.BLOCK:
                return False, ContentScanReason.PROMPT_INJECTION_DETECTED
            if result.decision != scan_decision.ALLOW:
                return False, ContentScanReason.SCANNER_ERROR
        return True, ContentScanReason.OK
    except Exception:
        return False, ContentScanReason.SCANNER_ERROR
