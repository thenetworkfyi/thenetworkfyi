"""Optional content scanner (opt-in defense-in-depth).

Enabled by settings.content_scan_enabled. When disabled, always passes.
Uses LLM Guard PromptInjection scanner; NOT a primary defense.
"""
from __future__ import annotations

from thenetwork.settings import get_settings

_scanner = None


def _get_scanner():
    """Create the optional scanner once, only when scanning is enabled."""
    global _scanner
    if _scanner is None:
        from llm_guard.input_scanners import PromptInjection
        from llm_guard.input_scanners.prompt_injection import MatchType

        _scanner = PromptInjection(match_type=MatchType.FULL)
    return _scanner


def scan_content(text: str) -> tuple[bool, str]:
    """Return (is_safe, reason). When disabled, always returns (True, 'disabled').

    When enabled, scanner failures fail closed: the defense is optional only
    while disabled, never an allow-through path after it was selected.
    """
    s = get_settings()
    if not s.content_scan_enabled:
        return True, "disabled"

    try:
        scanner = _get_scanner()
        sanitized, is_valid, _ = scanner.scan("", text)
        if not is_valid:
            return False, "prompt_injection_detected"
        return True, "ok"
    except Exception:
        return False, "scanner_error"
