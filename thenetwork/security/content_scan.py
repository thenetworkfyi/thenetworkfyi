"""Optional content scanner (opt-in defense-in-depth).

Enabled by settings.content_scan_enabled. When disabled, always passes.
Uses LLM Guard PromptInjection scanner; NOT a primary defense.
"""
from __future__ import annotations

from thenetwork.settings import get_settings


def scan_content(text: str) -> tuple[bool, str]:
    """Return (is_safe, reason). When disabled, always returns (True, 'disabled').

    Failure (import error, model not loaded) is treated as safe to avoid
    blocking the pipeline when the optional dependency is absent.
    """
    s = get_settings()
    if not s.content_scan_enabled:
        return True, "disabled"

    try:
        from llm_guard.input_scanners import PromptInjection
        from llm_guard.input_scanners.prompt_injection import MatchType

        scanner = PromptInjection(match_type=MatchType.FULL)
        sanitized, is_valid, _ = scanner.scan("", text)
        if not is_valid:
            return False, "prompt_injection_detected"
        return True, "ok"
    except Exception:
        # Optional dependency absent or model failed to load — allow through
        return True, "scanner_unavailable"
